"""可恢复且安全脱敏的 Review 编排流水线。"""

from __future__ import annotations

import hashlib
import asyncio
import json
import logging
import threading
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


logger = logging.getLogger(__name__)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _redact_json_response(value: str) -> str:
    """按字段脱敏 JSON 回复，避免对序列化文本二次替换破坏 JSON。"""
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return redact_secrets(value).text

    def sanitize(item: Any) -> Any:
        if isinstance(item, str):
            return redact_secrets(item).text
        if isinstance(item, list):
            return [sanitize(part) for part in item]
        if isinstance(item, dict):
            return {key: sanitize(part) for key, part in item.items()}
        return item

    return json.dumps(sanitize(payload), ensure_ascii=False, separators=(",", ":"))


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
        self._lease_token: str | None = None

    def _trace(self, run_id: str, *, kind: str, input_hash: str = "", prompt: str = "", response: str = "", model: str = "", tool_name: str = "", prompt_tokens: int = 0, completion_tokens: int = 0, cost_usd: float = 0.0, duration_ms: int = 0, error: str = "", parent_trace_id: str = "", rule_id: str = "", ruleset_hash: str = "", metadata: dict[str, Any] | None = None, prompt_is_sanitized: bool = False, response_is_json: bool = False) -> str:
        if self._lease_token:
            self.store.assert_run_lease(run_id, self._lease_token)
        trace_id = f"trace-{uuid4().hex}"
        safe_prompt = prompt if prompt_is_sanitized else redact_secrets(prompt).text
        safe_response = _redact_json_response(response) if response_is_json else redact_secrets(response).text
        self.store.save_trace(TraceRecord(trace_id=trace_id, run_id=run_id, kind=kind, input_hash=input_hash, prompt=safe_prompt, response=safe_response, model=model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, cost_usd=cost_usd, duration_ms=duration_ms, tool_name=tool_name, error=redact_secrets(error).text, parent_trace_id=parent_trace_id, rule_id=rule_id, ruleset_hash=ruleset_hash, metadata=metadata or {}))
        return trace_id

    def _save_checkpoint(self, run_id: str, stage: str, payload: dict[str, Any], **kwargs: Any) -> None:
        if self._lease_token:
            self.store.assert_run_lease(run_id, self._lease_token)
        self.store.save_checkpoint(run_id, stage, payload, **kwargs)

    def _update_run(self, run_id: str, **kwargs: Any) -> None:
        if self._lease_token:
            self.store.assert_run_lease(run_id, self._lease_token)
        self.store.update_run(run_id, **kwargs)

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
        logger.info("MDR 语言分组 run_id=%s languages=%s filter=%s",
                    run_id, ",".join(item.language for item in language_diffs) or "none",
                    language_filter or "none")
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
                try:
                    return self._run_mdr_rules(
                        run_id, request, sanitized_diff, diff_hash, config,
                        language_filter=language, manage_stale=False,
                    )
                except Exception as exc:
                    raise RuntimeError(f"language={language}: {exc}") from exc

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
                            metadata={"concurrent_batch_failure": True,
                                      "batch_identity": next(
                                          (part for part in error.split() if part.startswith("language=")),
                                          "unknown",
                                      )})
                degradations.append("mdr_batch_failed")
            if scheduled.errors:
                # 成功兄弟 checkpoint 已持久化；让 run 失败，恢复时只重试失败批次。
                raise RuntimeError("one or more concurrent MDR batches failed")
            return findings, list(dict.fromkeys(degradations))

        for language_diff in language_diffs:
            rules = self.rules.applicable(language_diff.language)
            if language_diff.language == "unknown" and not rules:
                self._trace(run_id, kind="mdr_batch", input_hash=language_diff.diff_hash,
                            prompt="未识别语言，未调用模型；原始 diff 仅用于诊断。",
                            response=language_diff.diff, error="unknown_language",
                            metadata={"language": "unknown", "files": list(language_diff.files),
                                      "rejections": ["unknown_language"],
                                      "trace_role": "diagnostic_diff"})
                degradations.append("unknown_language_skipped")
                continue
            if not rules:
                continue
            ruleset_hash = self.rules.ruleset_hash(language_diff.language)
            batches = build_rule_batches(language_diff, rules, max(256, config.max_diff_chars))
            for batch_index, batch in enumerate(batches):
                checkpoint_key = f"rules:{batch.language}:{batch_index}"
                logger.info("MDR 批次开始 language=%s batch=%d rules=%s files=%d",
                            batch.language, batch_index,
                            ",".join(rule.id for rule in batch.rules), len(batch.files))
                expected = {"ruleset_hash": ruleset_hash, "diff_hash": batch.diff_hash}
                for stale_stage in (checkpoint_key, checkpoint_key + ":reservation"):
                    stale_record = self.store.get_checkpoint_record(run_id, stale_stage)
                    if (stale_record and stale_record["status"] == "success" and
                            any(stale_record["payload"].get(key) != value for key, value in expected.items())):
                        self.store.supersede_checkpoint(run_id, stale_stage)
                saved = self.store.get_checkpoint(run_id, checkpoint_key)
                if saved and all(saved.get(key) == value for key, value in expected.items()):
                    # 旧版本可能把空响应和 JSON 解析拒绝误记为成功；恢复时
                    # 检查 reservation 的原始结果，避免永久复用空报告。
                    saved_reservation = self.store.get_checkpoint(run_id, checkpoint_key + ":reservation")
                    saved_token = saved_reservation.get("token") if saved_reservation else None
                    saved_result = self.store.get_reservation(run_id, saved_token) if saved_token else None
                    result_payload = (saved_result or {}).get("result") or {}
                    if not result_payload.get("rejections"):
                        logger.info("MDR checkpoint 命中 language=%s batch=%d findings=%d",
                                    batch.language, batch_index, len(saved.get("findings", [])))
                        findings.extend(Finding.from_dict(item) for item in saved.get("findings", []))
                        continue
                    logger.warning("MDR checkpoint 失效 language=%s batch=%d reason=previous_response_rejected",
                                   batch.language, batch_index)
                    self.store.supersede_checkpoint(run_id, checkpoint_key)
                    if saved_reservation:
                        self.store.supersede_checkpoint(run_id, checkpoint_key + ":reservation")

                reservation_key = checkpoint_key + ":reservation"
                reservation_state = self.store.get_checkpoint(run_id, reservation_key)
                # reservation checkpoint 是预算记账和外部模型请求之间的持久化交接点。
                if reservation_state and reservation_state.get("status") in ("pending", "in_flight", "completed") and all(reservation_state.get(key) == value for key, value in expected.items()):
                    if reservation_state.get("status") == "completed" and reservation_state.get("findings") is not None:
                        recovered_findings = [Finding.from_dict(item) for item in reservation_state.get("findings", [])]
                        self._save_checkpoint(run_id, checkpoint_key, {**expected, "findings": [item.to_dict() for item in recovered_findings], "batch_trace_id": reservation_state.get("batch_trace_id", ""), "finding_trace_ids": reservation_state.get("finding_trace_ids", [])})
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
                                prompt_is_sanitized=True, response_is_json=True,
                                metadata={"rule_ids": [rule.id for rule in batch.rules], "recovery": True,
                                          "rejections": result.get("rejections", [])})
                            recovered_findings = []
                            finding_trace_ids = []
                            for item in raw_findings:
                                finding = Finding.from_dict(item)
                                finding_trace_id = self._trace(run_id, kind="mdr_finding", input_hash=recovered_input_hash,
                                    prompt=recovered_prompt, response=response_text,
                                    model=result.get("model", reservation_state.get("model", "")),
                                    prompt_tokens=result.get("prompt_tokens", 0),
                                    completion_tokens=result.get("completion_tokens", 0),
                                    cost_usd=db_reservation.get("actual_usd", 0.0),
                                    prompt_is_sanitized=True, response_is_json=True,
                                    ruleset_hash=ruleset_hash, parent_trace_id=batch_trace_id,
                                    rule_id=finding.rule_id,
                                    metadata={"batch_trace_id": batch_trace_id, "recovery": True,
                                              "finding_body": finding.body})
                                recovered_findings.append(replace(finding, trace_id=finding_trace_id, confidence="advisory"))
                                finding_trace_ids.append(finding_trace_id)
                            self._save_checkpoint(run_id, reservation_key, {**reservation_state, **expected, "status": "completed", "findings": [item.to_dict() for item in recovered_findings], "batch_trace_id": batch_trace_id, "finding_trace_ids": finding_trace_ids})
                            self._save_checkpoint(run_id, checkpoint_key, {**expected, "findings": [item.to_dict() for item in recovered_findings], "batch_trace_id": batch_trace_id, "finding_trace_ids": finding_trace_ids})
                            findings.extend(recovered_findings)
                            continue
                    had_inflight = bool(db_reservation and db_reservation.get("status") == "in_flight")
                    if had_inflight:
                        self.store.settle_reservation(run_id, token, 0.0, owner_token=self._lease_token)
                    elif reservation_state.get("status") == "pending" and db_reservation is None:
                        self._save_checkpoint(run_id, reservation_key, {**reservation_state, **expected, "status": "recovered"})
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
                        self._save_checkpoint(run_id, checkpoint_key, {**expected, "findings": [], "batch_trace_id": trace_id, "finding_trace_ids": []})
                        self._save_checkpoint(run_id, reservation_key, {**reservation_state, **expected, "status": "recovered"})
                        continue

                prompt = build_rule_prompt(batch)
                # MDR 批次独立做预算决策，保证组织规则在其他 review 降级时仍可审计。
                decision = controller.select(config.model, estimate_tokens(prompt), allow_truncate=True)
                logger.info("MDR 预算决策 language=%s batch=%d model=%s allow=%s reason=%s truncated=%s",
                            batch.language, batch_index, decision.model or "none",
                            decision.allow_llm, decision.reason or "none", decision.truncate)
                if decision.reason != "within_budget":
                    degradations.append(decision.reason)
                if not decision.allow_llm or not decision.model:
                    trace_id = self._trace(run_id, kind="mdr_batch", input_hash=batch.diff_hash, prompt=prompt, model=decision.model or "", ruleset_hash=ruleset_hash, error=decision.reason or "llm_disabled", prompt_is_sanitized=True, metadata={"rule_ids": [rule.id for rule in batch.rules], "rejections": [decision.reason or "llm_disabled"]})
                    self._save_checkpoint(run_id, checkpoint_key, {**expected, "findings": [], "batch_trace_id": trace_id, "finding_trace_ids": []})
                    continue
                reservation = controller.reserve(decision.model, decision.estimated_tokens or estimate_tokens(prompt))
                # 先写 pending 再创建数据库 reservation；任一边界崩溃后都能确定性对账。
                self._save_checkpoint(run_id, reservation_key, {**expected, "status": "pending", "token": reservation.token if reservation else "", "reserved_usd": reservation.reserved_usd if reservation else 0.0, "model": decision.model})
                if reservation is None or not self.store.reserve_budget(run_id, reservation.token, reservation.reserved_usd, self._lease_token):
                    if reservation is not None:
                        controller.commit(reservation, 0.0)
                    degradations.append("budget_exceeded")
                    trace_id = self._trace(run_id, kind="mdr_batch", input_hash=batch.diff_hash, prompt=prompt, model=decision.model, ruleset_hash=ruleset_hash, error="budget_exceeded", prompt_is_sanitized=True, metadata={"rule_ids": [rule.id for rule in batch.rules], "rejections": ["budget_exceeded"]})
                    self._save_checkpoint(run_id, checkpoint_key, {**expected, "findings": [], "batch_trace_id": trace_id, "finding_trace_ids": []})
                    logger.warning("MDR 批次跳过 language=%s batch=%d reason=budget_exceeded",
                                   batch.language, batch_index)
                    continue
                self._save_checkpoint(run_id, reservation_key, {**expected, "status": "in_flight", "token": reservation.token, "reserved_usd": reservation.reserved_usd, "model": decision.model})
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
                        owner_token=self._lease_token,
                    )
                    response_valid = not parsed.rejections
                    accepted = accepted and persisted_accepted and response_valid
                    rejection_messages = list(parsed.rejections)
                    if not response_valid:
                        rejection_messages.append("invalid_mdr_response")
                    if not accepted:
                        if response_valid:
                            rejection_messages.append("provider_cost_exceeded_budget")
                    sent_prompt = prompt if decision.max_chars is None else prompt[:decision.max_chars]
                    duration_ms = int((time.monotonic() - started) * 1000)
                    batch_trace_id = self._trace(run_id, kind="mdr_batch", input_hash=_hash(sent_prompt), prompt=sent_prompt, response=response.text, model=response.model or decision.model, ruleset_hash=ruleset_hash, prompt_tokens=response.prompt_tokens, completion_tokens=response.completion_tokens, cost_usd=response.cost_usd, duration_ms=duration_ms, error="provider_cost_exceeded_budget" if not accepted else "", prompt_is_sanitized=True, response_is_json=True, metadata={"rule_ids": [rule.id for rule in batch.rules], "rejections": rejection_messages, "diff_hash": batch.diff_hash})
                    batch_findings: list[Finding] = []
                    finding_trace_ids: list[str] = []
                    if accepted:
                        for finding in parsed.findings:
                            finding_trace_id = self._trace(run_id, kind="mdr_finding", input_hash=_hash(sent_prompt), prompt=sent_prompt,
                                response=response.text, model=response.model or decision.model,
                                prompt_tokens=response.prompt_tokens, completion_tokens=response.completion_tokens,
                                cost_usd=response.cost_usd, duration_ms=duration_ms,
                                prompt_is_sanitized=True, response_is_json=True,
                                ruleset_hash=ruleset_hash, parent_trace_id=batch_trace_id,
                                rule_id=finding.rule_id,
                                metadata={"batch_trace_id": batch_trace_id, "finding_body": finding.body})
                            batch_findings.append(replace(finding, trace_id=finding_trace_id, confidence="advisory"))
                            finding_trace_ids.append(finding_trace_id)
                    elif not response_valid:
                        degradations.append("invalid_mdr_response")
                    else:
                        degradations.append("provider_cost_exceeded")
                    self._save_checkpoint(run_id, reservation_key, {**expected, "status": "completed" if accepted else "rejected", "token": reservation.token, "reserved_usd": reservation.reserved_usd, "model": decision.model, "findings": [item.to_dict() for item in batch_findings], "batch_trace_id": batch_trace_id, "finding_trace_ids": finding_trace_ids, "rejections": rejection_messages})
                    if accepted:
                        self._save_checkpoint(run_id, checkpoint_key, {**expected, "findings": [item.to_dict() for item in batch_findings], "batch_trace_id": batch_trace_id, "finding_trace_ids": finding_trace_ids})
                    findings.extend(batch_findings)
                    logger.info("MDR 批次完成 language=%s batch=%d findings=%d rejections=%d accepted=%s cost_usd=%.6f duration_ms=%d",
                                batch.language, batch_index, len(batch_findings),
                                len(rejection_messages), accepted, response.cost_usd,
                                int((time.monotonic() - started) * 1000))
                except Exception as exc:
                    logger.error("MDR 批次异常 language=%s batch=%d error_type=%s error=%s",
                                 batch.language, batch_index, type(exc).__name__,
                                 redact_secrets(str(exc)).text)
                    db_reservation = self.store.get_reservation(run_id, reservation.token)
                    if db_reservation and db_reservation.get("status") == "completed":
                        # provider 已完成且成本已结算，仅后续落盘失败；保留 completed
                        # 让恢复逻辑从 result_json 重建，绝不能重新发请求。
                        self._save_checkpoint(
                            run_id, reservation_key,
                            {**expected, "status": "completed", "token": reservation.token,
                             "reserved_usd": reservation.reserved_usd, "model": decision.model},
                        )
                    else:
                        controller.commit(reservation, 0.0)
                        self.store.settle_reservation(run_id, reservation.token, 0.0, owner_token=self._lease_token)
                        # 已明确收到 provider 异常时允许恢复重试；只有进程突然中断
                        # 留下的 in_flight 状态才按未知结果保守处理。
                        self._save_checkpoint(
                            run_id,
                            reservation_key,
                            {**expected, "status": "failed", "token": reservation.token,
                             "reserved_usd": reservation.reserved_usd, "model": decision.model,
                             "error": redact_secrets(str(exc)).text},
                            status="failed",
                        )
                    raise
        return findings, degradations

    def _fail(self, run_id: str, stage: str, exc: Exception) -> None:
        logger.error("阶段失败 stage=%s run_id=%s error_type=%s error=%s",
                     stage, run_id, type(exc).__name__, redact_secrets(str(exc)).text)
        self._save_checkpoint(run_id, stage, {"error": str(exc)}, status="failed")
        self._trace(run_id, kind="stage_error", error=f"{stage}: {exc}")
        self._update_run(run_id, status="failed")

    def run(self, url_or_run_id: str) -> ReviewResult:
        """按 ID 恢复已有 run，或创建新 run，并只执行缺失阶段。"""
        resumed = True
        try:
            run = self.store.get_run(url_or_run_id)
            run_id = url_or_run_id
            config = RunConfig.from_dict(run["config"])
        except KeyError:
            resumed = False
            config = replace(self.config, url=url_or_run_id)
            run_id = self.store.create_run(config)
            run = self.store.get_run(run_id)

        logger.info("评审开始 run_id=%s resumed=%s adapter=%s mode=%s budget_usd=%.4f",
                    run_id, resumed, type(self.adapter).__name__, config.review_mode,
                    config.budget_usd)

        lease_token = str(uuid4())
        self._lease_token = lease_token
        lease_ttl = max(120, int(config.llm_timeout_seconds * 2 + 60))
        if not self.store.acquire_run_lease(run_id, lease_token, lease_ttl):
            raise RuntimeError(f"run {run_id} is already active")
        heartbeat_stop = threading.Event()

        def heartbeat() -> None:
            interval = min(30.0, max(5.0, lease_ttl / 3))
            while not heartbeat_stop.wait(interval):
                if not self.store.refresh_run_lease(run_id, lease_token, lease_ttl):
                    return

        heartbeat_thread = threading.Thread(target=heartbeat, name=f"review-lease-{run_id[:8]}", daemon=True)
        heartbeat_thread.start()
        current_stage = "fetch"
        failure_recorded = False
        try:
            fetch = self.store.get_checkpoint(run_id, "fetch")
            logger.info("阶段开始 stage=fetch run_id=%s checkpoint_hit=%s",
                        run_id, fetch is not None)
            if fetch is None:
                current_stage = "fetch"
                request = self.adapter.fetch(config.url)
                fetch = {"request": request.to_dict(), "diff_hash": _hash(request.diff)}
                self._save_checkpoint(run_id, "fetch", fetch)
            request = ChangeRequest.from_dict(fetch["request"])
            logger.info("阶段完成 stage=fetch run_id=%s source=%s diff_chars=%d",
                        run_id, request.source, len(request.diff))

            sanitize = self.store.get_checkpoint(run_id, "sanitize")
            logger.info("阶段开始 stage=sanitize run_id=%s checkpoint_hit=%s",
                        run_id, sanitize is not None)
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
                self._save_checkpoint(run_id, "sanitize", sanitize)
            sanitized_request = ChangeRequest.from_dict(sanitize["request"])
            sanitized_diff = sanitize["diff"]
            diff_hash = sanitize.get("diff_hash", _hash(request.diff))
            logger.info("阶段完成 stage=sanitize run_id=%s redactions=%d sanitized_chars=%d",
                        run_id, len(sanitize.get("redactions", [])), len(sanitized_diff))

            tools_payload = self.store.get_checkpoint(run_id, "tools")
            logger.info("阶段开始 stage=tools run_id=%s checkpoint_hit=%s tools=%d",
                        run_id, tools_payload is not None, len(self.tools.specs))
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
                self._save_checkpoint(run_id, "tools", tools_payload)
            tool_findings = [Finding.from_dict(item) for item in tools_payload.get("findings", [])]
            logger.info("阶段完成 stage=tools run_id=%s findings=%d",
                        run_id, len(tool_findings))

            current_stage = "review"
            logger.info("阶段开始 stage=review run_id=%s mode=%s",
                        run_id, config.review_mode)
            # v1 is intentionally MDR-only: the model is called only from the
            # rule batches above, never with an unconstrained generic prompt.
            mdr_findings, mdr_degradations = self._run_mdr_rules(run_id, request, sanitized_diff, diff_hash, config)
            review_payload = self.store.get_checkpoint(run_id, "review")
            if review_payload is None:
                review_payload = {"findings": [], "degradations": [], "mode": "mdr_only"}
                self._save_checkpoint(run_id, "review", review_payload)
            llm_findings: list[Finding] = []
            degradations = list(dict.fromkeys(mdr_degradations + list(review_payload.get("degradations", []))))
            logger.info("阶段完成 stage=review run_id=%s mdr_findings=%d degradations=%d",
                        run_id, len(mdr_findings), len(degradations))

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
                      or bool(trace.get("error"))
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
                logger.info("阶段开始 stage=render run_id=%s checkpoint_hit=false",
                            run_id)
                from .report import render_markdown
                result.markdown = render_markdown(result)
                self._save_checkpoint(run_id, "render", {"markdown": result.markdown, "input_hash": render_input_hash})
            else:
                logger.info("阶段开始 stage=render run_id=%s checkpoint_hit=true",
                            run_id)
                result.markdown = render.get("markdown", "")
            logger.info("阶段完成 stage=render run_id=%s markdown_chars=%d",
                        run_id, len(result.markdown))
            self._update_run(run_id, status="completed")
            logger.info("评审完成 run_id=%s findings=%d traces=%d cost_usd=%.6f",
                        run_id, len(findings), len(traces), result.cost_usd)
            return result
        except Exception as exc:
            if not failure_recorded:
                self._fail(run_id, current_stage, exc)
            raise
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=1)
            self.store.release_run_lease(run_id, lease_token)
            self._lease_token = None
            logger.debug("运行锁释放 run_id=%s", run_id)
