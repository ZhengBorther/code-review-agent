"""可恢复且安全脱敏的 Review 编排流水线。"""

from __future__ import annotations

import hashlib
import asyncio
import time
from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

from .budget import BudgetController
from .async_batches import AsyncBatchConfig, run_batches_async
from .llm import LLMClient, estimate_tokens
from .models import ChangeRequest, Finding, RunConfig, TraceRecord
from .diff_languages import split_diff_by_language
from .rule_review import build_rule_batches, build_rule_prompt, parse_rule_response
from .rules import RuleRegistry
from .security import redact_secrets
from .storage import StateStore
from .tools import ToolRegistry


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass
class ReviewResult:
    run_id: str
    request: ChangeRequest
    findings: list[Finding]
    traces: list[dict[str, Any]]
    cost_usd: float
    budget_usd: float
    degradations: list[str]
    markdown: str = ""


class ReviewPipeline:
    STAGES = ("fetch", "sanitize", "tools", "review", "render")

    def __init__(self, store: StateStore, adapter: Any, tools: ToolRegistry, llm: LLMClient, config: RunConfig, rules: RuleRegistry | None = None):
        self.store = store
        self.adapter = adapter
        self.tools = tools
        self.llm = llm
        self.config = config
        self.rules = rules

    def _trace(self, run_id: str, *, kind: str, input_hash: str = "", prompt: str = "", response: str = "", model: str = "", tool_name: str = "", prompt_tokens: int = 0, completion_tokens: int = 0, cost_usd: float = 0.0, duration_ms: int = 0, error: str = "", parent_trace_id: str = "", rule_id: str = "", ruleset_hash: str = "", metadata: dict[str, Any] | None = None) -> str:
        trace_id = f"trace-{uuid4().hex}"
        self.store.save_trace(TraceRecord(trace_id=trace_id, run_id=run_id, kind=kind, input_hash=input_hash, prompt=redact_secrets(prompt).text, response=redact_secrets(response).text, model=model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, cost_usd=cost_usd, duration_ms=duration_ms, tool_name=tool_name, error=redact_secrets(error).text, parent_trace_id=parent_trace_id, rule_id=rule_id, ruleset_hash=ruleset_hash, metadata=metadata or {}))
        return trace_id

    def _reclaim_orphan_reservations(self, run_id: str) -> list[str]:
        # 没有关联 checkpoint 的 reservation 无法确认是否仍由工作进程使用，
        # 因此保留预算并提示人工恢复，不根据经过时间做危险猜测。
        known_tokens = {
            item["payload"].get("token")
            for item in self.store.list_checkpoints(run_id, "rules:")
            if item["stage"].endswith(":reservation") and item["payload"].get("token")
        }
        degradations: list[str] = []
        for reservation in self.store.list_inflight_reservations(run_id):
            if reservation["token"] in known_tokens:
                continue
            self._trace(run_id, kind="mdr_batch", error="orphan_reservation_requires_manual_recovery", metadata={"token": reservation["token"], "recovery": True})
            degradations.append("orphan_reservation_requires_manual_recovery")
        return degradations

    def _run_mdr_rules(
        self,
        run_id: str,
        request: ChangeRequest,
        sanitized_diff: str,
        diff_hash: str,
        config: RunConfig,
        *,
        language_filter: str | None = None,
        manage_stale: bool = True,
    ) -> tuple[list[Finding], list[str]]:
        """执行各语言 MDR 批次，同时维护恢复状态和审计关联。"""
        if self.rules is None:
            return [], []
        controller = BudgetController(config)
        controller.spent_usd = float(self.store.get_run(run_id)["cost_usd"])
        findings: list[Finding] = []
        degradations: list[str] = self._reclaim_orphan_reservations(run_id) if manage_stale else []
        language_diffs = split_diff_by_language(sanitized_diff, config.profile_skip_globs)
        if language_filter is not None:
            language_diffs = tuple(item for item in language_diffs if item.language == language_filter)
        valid_stages: set[str] = set()
        # 先计算本次应存在的阶段，再把已删除的规则或文件标为过期并释放预算。
        for language_diff in language_diffs:
            rules = self.rules.applicable(language_diff.language)
            if language_diff.language == "unknown" and not rules:
                continue
            for index, _batch in enumerate(build_rule_batches(language_diff, rules, max(256, config.max_diff_chars))):
                valid_stages.add(f"rules:{language_diff.language}:{index}")
        if manage_stale:
            for checkpoint in self.store.list_checkpoints(run_id, "rules:"):
                stage = checkpoint["stage"]
                base = stage[:-len(":reservation")] if stage.endswith(":reservation") else stage
                if base not in valid_stages:
                    self.store.supersede_checkpoint(run_id, stage)

        # 不同语言拥有不同 checkpoint key，可以安全并发；SQLite reservation
        # 仍负责跨线程的单 PR 预算原子性。单语言内部保持稳定的批次顺序。
        if manage_stale and language_filter is None and len(language_diffs) > 1 and config.max_concurrency > 1:
            languages = tuple(item.language for item in language_diffs)

            def review_language(language: str) -> tuple[list[Finding], list[str]]:
                return self._run_mdr_rules(
                    run_id, request, sanitized_diff, diff_hash, config,
                    language_filter=language, manage_stale=False,
                )

            scheduled = asyncio.run(run_batches_async(
                languages,
                review_language,
                AsyncBatchConfig(config.max_concurrency),
            ))
            for language_findings, language_degradations in scheduled.results:
                findings.extend(language_findings)
                degradations.extend(language_degradations)
            for error in scheduled.errors:
                self._trace(run_id, kind="mdr_batch", error=error,
                            metadata={"concurrent_batch_failure": True})
                degradations.append("mdr_batch_failed")
            return findings, list(dict.fromkeys(degradations))

        for language_diff in language_diffs:
            rules = self.rules.applicable(language_diff.language)
            if language_diff.language == "unknown" and not rules:
                self._trace(run_id, kind="mdr_batch", input_hash=language_diff.diff_hash,
                            response=language_diff.diff, error="unknown_language",
                            metadata={"language": "unknown", "files": list(language_diff.files),
                                      "rejections": ["unknown_language"]})
                degradations.append("unknown_language_skipped")
                continue
            if not rules:
                continue
            ruleset_hash = self.rules.ruleset_hash(language_diff.language)
            batches = build_rule_batches(language_diff, rules, max(256, config.max_diff_chars))
            for batch_index, batch in enumerate(batches):
                checkpoint_key = f"rules:{batch.language}:{batch_index}"
                expected = {"ruleset_hash": ruleset_hash, "diff_hash": batch.diff_hash}
                for stale_stage in (checkpoint_key, checkpoint_key + ":reservation"):
                    stale_record = self.store.get_checkpoint_record(run_id, stale_stage)
                    if (stale_record and stale_record["status"] == "success" and
                            any(stale_record["payload"].get(key) != value for key, value in expected.items())):
                        self.store.supersede_checkpoint(run_id, stale_stage)
                saved = self.store.get_checkpoint(run_id, checkpoint_key)
                if saved and all(saved.get(key) == value for key, value in expected.items()):
                    findings.extend(Finding.from_dict(item) for item in saved.get("findings", []))
                    continue

                reservation_key = checkpoint_key + ":reservation"
                reservation_state = self.store.get_checkpoint(run_id, reservation_key)
                # reservation checkpoint 是预算记账和外部模型请求之间的持久化交接点。
                if reservation_state and reservation_state.get("status") in ("pending", "in_flight", "completed") and all(reservation_state.get(key) == value for key, value in expected.items()):
                    if reservation_state.get("status") == "completed" and reservation_state.get("findings") is not None:
                        recovered_findings = [Finding.from_dict(item) for item in reservation_state.get("findings", [])]
                        self.store.save_checkpoint(run_id, checkpoint_key, {**expected, "findings": [item.to_dict() for item in recovered_findings], "batch_trace_id": reservation_state.get("batch_trace_id", ""), "finding_trace_ids": reservation_state.get("finding_trace_ids", [])})
                        findings.extend(recovered_findings)
                        continue
                    token = reservation_state.get("token")
                    db_reservation = self.store.get_reservation(run_id, token) if token else None
                    # 即使 trace 或 checkpoint 写入被中断，结算仍是持久化的；
                    # 从原子保存的模型回复重建 finding，不能静默返回空结果。
                    if db_reservation and db_reservation.get("status") == "completed":
                        # 模型回复与结算在同一事务中保存；进程若随后退出，
                        # 这里以数据库结果为准，避免重复请求并恢复 finding。
                        result = db_reservation.get("result") or {}
                        response_text = result.get("response", "")
                        parsed_items = result.get("parsed_findings") or []
                        if response_text or parsed_items:
                            parsed = parse_rule_response(response_text, batch) if response_text else None
                            raw_findings = parsed_items or ([item.to_dict() for item in parsed.findings] if parsed else [])
                            recovered_prompt = result.get("prompt", build_rule_prompt(batch))
                            recovered_input_hash = _hash(recovered_prompt)
                            batch_trace_id = self._trace(run_id, kind="mdr_batch", input_hash=recovered_input_hash,
                                prompt=recovered_prompt, response=response_text,
                                model=result.get("model", reservation_state.get("model", "")),
                                ruleset_hash=ruleset_hash, prompt_tokens=result.get("prompt_tokens", 0),
                                completion_tokens=result.get("completion_tokens", 0), cost_usd=db_reservation.get("actual_usd", 0.0),
                                metadata={"rule_ids": [rule.id for rule in batch.rules], "recovery": True,
                                          "rejections": result.get("rejections", [])})
                            recovered_findings = []
                            finding_trace_ids = []
                            for item in raw_findings:
                                finding = Finding.from_dict(item)
                                finding_trace_id = self._trace(run_id, kind="mdr_finding", input_hash=recovered_input_hash,
                                    response=finding.body, ruleset_hash=ruleset_hash, parent_trace_id=batch_trace_id,
                                    rule_id=finding.rule_id, metadata={"batch_trace_id": batch_trace_id, "recovery": True})
                                recovered_findings.append(replace(finding, trace_id=finding_trace_id, confidence="advisory"))
                                finding_trace_ids.append(finding_trace_id)
                            self.store.save_checkpoint(run_id, reservation_key, {**reservation_state, **expected, "status": "completed", "findings": [item.to_dict() for item in recovered_findings], "batch_trace_id": batch_trace_id, "finding_trace_ids": finding_trace_ids})
                            self.store.save_checkpoint(run_id, checkpoint_key, {**expected, "findings": [item.to_dict() for item in recovered_findings], "batch_trace_id": batch_trace_id, "finding_trace_ids": finding_trace_ids})
                            findings.extend(recovered_findings)
                            continue
                    had_inflight = bool(db_reservation and db_reservation.get("status") == "in_flight")
                    if had_inflight:
                        self.store.settle_reservation(run_id, token, 0.0)
                    elif reservation_state.get("status") == "pending" and db_reservation is None:
                        self.store.save_checkpoint(run_id, reservation_key, {**reservation_state, **expected, "status": "recovered"})
                        reservation_state = None
                    else:
                        db_reservation = None
                    if reservation_state is None:
                        pass
                    elif reservation_state.get("status") == "pending" and not had_inflight:
                        continue
                    else:
                        trace_id = self._trace(run_id, kind="mdr_batch", input_hash=batch.diff_hash, model=reservation_state.get("model", ""), ruleset_hash=ruleset_hash, error="previous MDR reservation was unresolved; skipped duplicate call", metadata={"rule_ids": [rule.id for rule in batch.rules], "recovery": True, "rejections": ["inflight_reservation_recovered"]})
                        degradations.append("inflight_reservation_recovered")
                        self.store.save_checkpoint(run_id, checkpoint_key, {**expected, "findings": [], "batch_trace_id": trace_id, "finding_trace_ids": []})
                        self.store.save_checkpoint(run_id, reservation_key, {**reservation_state, **expected, "status": "recovered"})
                        continue

                prompt = build_rule_prompt(batch)
                # MDR 批次独立做预算决策，保证组织规则在其他 review 降级时仍可审计。
                decision = controller.select(config.model, estimate_tokens(prompt), allow_truncate=True)
                if decision.reason != "within_budget":
                    degradations.append(decision.reason)
                if not decision.allow_llm or not decision.model:
                    trace_id = self._trace(run_id, kind="mdr_batch", input_hash=batch.diff_hash, prompt=prompt, model=decision.model or "", ruleset_hash=ruleset_hash, error=decision.reason or "llm_disabled", metadata={"rule_ids": [rule.id for rule in batch.rules], "rejections": [decision.reason or "llm_disabled"]})
                    self.store.save_checkpoint(run_id, checkpoint_key, {**expected, "findings": [], "batch_trace_id": trace_id, "finding_trace_ids": []})
                    continue
                reservation = controller.reserve(decision.model, decision.estimated_tokens or estimate_tokens(prompt))
                # 先写 pending 再创建数据库 reservation；任一边界崩溃后都能确定性对账。
                self.store.save_checkpoint(run_id, reservation_key, {**expected, "status": "pending", "token": reservation.token if reservation else "", "reserved_usd": reservation.reserved_usd if reservation else 0.0, "model": decision.model})
                if reservation is None or not self.store.reserve_budget(run_id, reservation.token, reservation.reserved_usd):
                    if reservation is not None:
                        controller.commit(reservation, 0.0)
                    degradations.append("budget_exceeded")
                    trace_id = self._trace(run_id, kind="mdr_batch", input_hash=batch.diff_hash, prompt=prompt, model=decision.model, ruleset_hash=ruleset_hash, error="budget_exceeded", metadata={"rule_ids": [rule.id for rule in batch.rules], "rejections": ["budget_exceeded"]})
                    self.store.save_checkpoint(run_id, checkpoint_key, {**expected, "findings": [], "batch_trace_id": trace_id, "finding_trace_ids": []})
                    continue
                self.store.save_checkpoint(run_id, reservation_key, {**expected, "status": "in_flight", "token": reservation.token, "reserved_usd": reservation.reserved_usd, "model": decision.model})
                started = time.monotonic()
                try:
                    # 模型只接收已脱敏且受长度限制的 prompt；供应商回复一律按不可信 JSON 解析。
                    response = self.llm.review(prompt, decision.model, max_chars=decision.max_chars, max_tokens=decision.max_tokens or config.completion_tokens)
                    parsed = parse_rule_response(response.text, batch)
                    accepted = controller.commit(reservation, response.cost_usd)
                    persisted_accepted = self.store.settle_reservation(
                        run_id, reservation.token, response.cost_usd,
                        {"response": response.text, "parsed_findings": [item.to_dict() for item in parsed.findings],
                         "rejections": list(parsed.rejections), "prompt": prompt if decision.max_chars is None else prompt[:decision.max_chars],
                         "model": response.model or decision.model, "prompt_tokens": response.prompt_tokens,
                         "completion_tokens": response.completion_tokens},
                    )
                    accepted = accepted and persisted_accepted
                    rejection_messages = list(parsed.rejections)
                    if not accepted:
                        rejection_messages.append("provider_cost_exceeded_budget")
                    sent_prompt = prompt if decision.max_chars is None else prompt[:decision.max_chars]
                    batch_trace_id = self._trace(run_id, kind="mdr_batch", input_hash=_hash(sent_prompt), prompt=sent_prompt, response=response.text, model=response.model or decision.model, ruleset_hash=ruleset_hash, prompt_tokens=response.prompt_tokens, completion_tokens=response.completion_tokens, cost_usd=response.cost_usd, duration_ms=int((time.monotonic() - started) * 1000), error="provider_cost_exceeded_budget" if not accepted else "", metadata={"rule_ids": [rule.id for rule in batch.rules], "rejections": rejection_messages, "diff_hash": batch.diff_hash})
                    batch_findings: list[Finding] = []
                    finding_trace_ids: list[str] = []
                    if accepted:
                        for finding in parsed.findings:
                            finding_trace_id = self._trace(run_id, kind="mdr_finding", input_hash=batch.diff_hash, response=finding.body, ruleset_hash=ruleset_hash, parent_trace_id=batch_trace_id, rule_id=finding.rule_id, metadata={"batch_trace_id": batch_trace_id})
                            batch_findings.append(replace(finding, trace_id=finding_trace_id, confidence="advisory"))
                            finding_trace_ids.append(finding_trace_id)
                    else:
                        degradations.append("provider_cost_exceeded")
                    self.store.save_checkpoint(run_id, reservation_key, {**expected, "status": "completed" if accepted else "rejected", "token": reservation.token, "reserved_usd": reservation.reserved_usd, "model": decision.model, "findings": [item.to_dict() for item in batch_findings], "batch_trace_id": batch_trace_id, "finding_trace_ids": finding_trace_ids})
                    self.store.save_checkpoint(run_id, checkpoint_key, {**expected, "findings": [item.to_dict() for item in batch_findings], "batch_trace_id": batch_trace_id, "finding_trace_ids": finding_trace_ids})
                    findings.extend(batch_findings)
                except Exception:
                    controller.commit(reservation, 0.0)
                    self.store.settle_reservation(run_id, reservation.token, 0.0)
                    raise
        return findings, degradations

    def _fail(self, run_id: str, stage: str, exc: Exception) -> None:
        self.store.save_checkpoint(run_id, stage, {"error": str(exc)}, status="failed")
        self._trace(run_id, kind="stage_error", error=f"{stage}: {exc}")
        self.store.update_run(run_id, status="failed")

    def run(self, url_or_run_id: str) -> ReviewResult:
        """按 ID 恢复已有 run，或创建新 run，并只执行缺失阶段。"""
        try:
            run = self.store.get_run(url_or_run_id)
            run_id = url_or_run_id
            config = RunConfig.from_dict(run["config"])
        except KeyError:
            config = replace(self.config, url=url_or_run_id)
            run_id = self.store.create_run(config)
            run = self.store.get_run(run_id)

        current_stage = "fetch"
        failure_recorded = False
        try:
            fetch = self.store.get_checkpoint(run_id, "fetch")
            if fetch is None:
                current_stage = "fetch"
                request = self.adapter.fetch(config.url)
                fetch = {"request": request.to_dict(), "diff_hash": _hash(request.diff)}
                self.store.save_checkpoint(run_id, "fetch", fetch)
            request = ChangeRequest.from_dict(fetch["request"])

            sanitize = self.store.get_checkpoint(run_id, "sanitize")
            if sanitize is None:
                current_stage = "sanitize"
                redacted = redact_secrets(request.diff)
                sanitized_request = replace(request, diff=redacted.text)
                sanitize = {
                    "request": sanitized_request.to_dict(),
                    "diff": redacted.text,
                    "diff_hash": fetch.get("diff_hash", _hash(request.diff)),
                    "redactions": [match.__dict__ for match in redacted.matches],
                }
                self.store.save_checkpoint(run_id, "sanitize", sanitize)
            sanitized_request = ChangeRequest.from_dict(sanitize["request"])
            sanitized_diff = sanitize["diff"]
            diff_hash = sanitize.get("diff_hash", _hash(request.diff))

            tools_payload = self.store.get_checkpoint(run_id, "tools")
            if tools_payload is None:
                current_stage = "tools"
                tool_findings: list[Finding] = []
                for spec in self.tools.specs:
                    started = time.monotonic()
                    try:
                        produced = spec.runner(sanitized_request, sanitized_diff)
                        for finding in produced:
                            trace_id = self._trace(run_id, kind="tool", tool_name=spec.name, input_hash=diff_hash, prompt=f"{spec.description}\n{sanitized_diff}", response=finding.body, duration_ms=int((time.monotonic() - started) * 1000))
                            tool_findings.append(replace(finding, confidence=spec.confidence, trace_id=trace_id))
                    except Exception as exc:
                        self._fail(run_id, "tools", exc)
                        failure_recorded = True
                        raise
                tools_payload = {"findings": [finding.to_dict() for finding in tool_findings]}
                self.store.save_checkpoint(run_id, "tools", tools_payload)
            tool_findings = [Finding.from_dict(item) for item in tools_payload.get("findings", [])]

            current_stage = "review"
            # v1 is intentionally MDR-only: the model is called only from the
            # rule batches above, never with an unconstrained generic prompt.
            mdr_findings, mdr_degradations = self._run_mdr_rules(run_id, request, sanitized_diff, diff_hash, config)
            review_payload = self.store.get_checkpoint(run_id, "review")
            if review_payload is None:
                review_payload = {"findings": [], "degradations": [], "mode": "mdr_only"}
                self.store.save_checkpoint(run_id, "review", review_payload)
            llm_findings: list[Finding] = []
            degradations = list(dict.fromkeys(mdr_degradations + list(review_payload.get("degradations", []))))

            findings = tool_findings + mdr_findings + llm_findings
            traces = self.store.get_traces(run_id)
            render = self.store.get_checkpoint(run_id, "render")
            active_trace_ids: list[str] = []
            for checkpoint in self.store.list_checkpoints(run_id, "rules:"):
                if checkpoint["stage"].endswith(":reservation"):
                    continue
                payload = checkpoint["payload"]
                if self.store.get_checkpoint(run_id, checkpoint["stage"]) is None:
                    continue
                if payload.get("batch_trace_id"):
                    active_trace_ids.append(payload["batch_trace_id"])
                active_trace_ids.extend(payload.get("finding_trace_ids", []))
            active_trace_ids = [trace_id for trace_id in active_trace_ids if trace_id]
            active_set = set(active_trace_ids)
            traces = [trace for trace in traces
                      if trace.get("kind") not in ("mdr_batch", "mdr_finding")
                      or trace.get("trace_id") in active_set
                      or (trace.get("kind") == "mdr_batch" and
                          "unknown_language" in (trace.get("metadata") or {}).get("rejections", []))]
            result = ReviewResult(run_id=run_id, request=request, findings=findings, traces=traces, cost_usd=float(self.store.get_run(run_id)["cost_usd"]), budget_usd=config.budget_usd, degradations=degradations)
            diagnostic_trace_ids = [trace.get("trace_id", "") for trace in traces
                                    if trace.get("kind") == "mdr_batch" and
                                    "unknown_language" in (trace.get("metadata") or {}).get("rejections", [])]
            render_input_hash = _hash("|".join(sorted(item.trace_id for item in findings) +
                                             sorted(active_trace_ids + diagnostic_trace_ids) +
                                             sorted(degradations)))
            if render is None or render.get("input_hash") != render_input_hash:
                current_stage = "render"
                from .report import render_markdown
                result.markdown = render_markdown(result)
                self.store.save_checkpoint(run_id, "render", {"markdown": result.markdown, "input_hash": render_input_hash})
            else:
                result.markdown = render.get("markdown", "")
            self.store.update_run(run_id, status="completed")
            return result
        except Exception as exc:
            if not failure_recorded:
                self._fail(run_id, current_stage, exc)
            raise
