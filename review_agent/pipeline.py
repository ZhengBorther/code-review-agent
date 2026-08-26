"""Resumable, secret-safe review orchestration."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

from .budget import BudgetController
from .llm import LLMClient, estimate_tokens
from .models import ChangeRequest, Finding, RunConfig, TraceRecord
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

    def __init__(self, store: StateStore, adapter: Any, tools: ToolRegistry, llm: LLMClient, config: RunConfig):
        self.store = store
        self.adapter = adapter
        self.tools = tools
        self.llm = llm
        self.config = config

    def _trace(self, run_id: str, *, kind: str, input_hash: str = "", prompt: str = "", response: str = "", model: str = "", tool_name: str = "", prompt_tokens: int = 0, completion_tokens: int = 0, cost_usd: float = 0.0, duration_ms: int = 0, error: str = "") -> str:
        trace_id = f"trace-{uuid4().hex}"
        self.store.save_trace(TraceRecord(trace_id=trace_id, run_id=run_id, kind=kind, input_hash=input_hash, prompt=redact_secrets(prompt).text, response=redact_secrets(response).text, model=model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, cost_usd=cost_usd, duration_ms=duration_ms, tool_name=tool_name, error=redact_secrets(error).text))
        return trace_id

    def _fail(self, run_id: str, stage: str, exc: Exception) -> None:
        self.store.save_checkpoint(run_id, stage, {"error": str(exc)}, status="failed")
        self._trace(run_id, kind="stage_error", error=f"{stage}: {exc}")
        self.store.update_run(run_id, status="failed")

    def run(self, url_or_run_id: str) -> ReviewResult:
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

            review_payload = self.store.get_checkpoint(run_id, "review")
            if review_payload is None:
                current_stage = "review"
                llm_findings: list[Finding] = []
                degradations: list[str] = []
                reservation_state = self.store.get_checkpoint(run_id, "review_reservation")
                if reservation_state and reservation_state.get("status") in ("in_flight", "completed"):
                    trace_id = self._trace(run_id, kind="llm_recovery", input_hash=diff_hash, model=reservation_state.get("model", ""), error="previous LLM reservation was unresolved; skipped duplicate call")
                    llm_findings.append(Finding(title="模型审查中断，未重复调用", body="检测到上次调用的持久化 reservation，已跳过可能重复计费的重试。", confidence="advisory", evidence="checkpoint:review_reservation", trace_id=trace_id))
                    degradations.append("inflight_reservation_recovered")
                    review_payload = {"findings": [finding.to_dict() for finding in llm_findings], "degradations": degradations}
                    self.store.save_checkpoint(run_id, "review", review_payload)
                    self.store.save_checkpoint(run_id, "review_reservation", {**reservation_state, "status": "recovered"})
                else:
                    controller = BudgetController(config)
                    controller.spent_usd = float(self.store.get_run(run_id)["cost_usd"])
                    bounded_diff = sanitized_diff[: config.max_diff_chars]
                    prompt = "请审查以下已脱敏的代码变更，指出潜在问题并给出修复建议。\n\n" + bounded_diff
                    decision = controller.select(config.model, estimate_tokens(prompt), allow_truncate=True)
                    if decision.reason != "within_budget":
                        degradations.append(decision.reason)
                    if decision.allow_llm and decision.model:
                        reservation = controller.reserve(decision.model, decision.estimated_tokens or estimate_tokens(prompt))
                        if reservation is None or not self.store.reserve_budget(run_id, reservation.token, reservation.reserved_usd):
                            decision = replace(decision, model=None, allow_llm=False, reason="budget_exceeded")
                            degradations.append("budget_exceeded")
                        else:
                            self.store.save_checkpoint(run_id, "review_reservation", {"status": "in_flight", "token": reservation.token, "reserved_usd": reservation.reserved_usd, "model": decision.model, "input_hash": diff_hash})
                            started = time.monotonic()
                            try:
                                response = self.llm.review(prompt, decision.model, max_chars=decision.max_chars, max_tokens=decision.max_tokens or config.completion_tokens)
                                try:
                                    accepted = controller.commit(reservation, response.cost_usd)
                                except Exception:
                                    accepted = False
                                persisted_accepted = self.store.settle_reservation(run_id, reservation.token, response.cost_usd)
                                accepted = accepted and persisted_accepted
                                if accepted:
                                    trace_id = self._trace(run_id, kind="llm", input_hash=diff_hash, prompt=prompt if decision.max_chars is None else prompt[: decision.max_chars], response=response.text, model=response.model or decision.model, prompt_tokens=response.prompt_tokens, completion_tokens=response.completion_tokens, cost_usd=response.cost_usd, duration_ms=int((time.monotonic() - started) * 1000))
                                    llm_findings.append(Finding(title="模型审查建议", body=response.text, confidence="advisory", evidence="llm", trace_id=trace_id))
                                    self.store.save_checkpoint(run_id, "review_reservation", {"status": "completed", "token": reservation.token, "reserved_usd": reservation.reserved_usd, "model": decision.model, "input_hash": diff_hash})
                                else:
                                    trace_id = self._trace(run_id, kind="llm", input_hash=diff_hash, prompt=prompt if decision.max_chars is None else prompt[: decision.max_chars], response=response.text, model=response.model or decision.model, prompt_tokens=response.prompt_tokens, completion_tokens=response.completion_tokens, cost_usd=response.cost_usd, duration_ms=int((time.monotonic() - started) * 1000), error="provider_cost_exceeded_budget")
                                    llm_findings.append(Finding(title="模型审查超出预算", body="模型实际成本超过剩余预算，回复未作为审查建议采纳。", confidence="advisory", evidence="budget:provider_cost_exceeded", trace_id=trace_id))
                                    degradations.append("provider_cost_exceeded")
                                    self.store.save_checkpoint(run_id, "review_reservation", {"status": "rejected", "token": reservation.token, "reserved_usd": reservation.reserved_usd, "model": decision.model, "input_hash": diff_hash})
                            except Exception as exc:
                                controller.commit(reservation, 0.0)
                                self.store.settle_reservation(run_id, reservation.token, 0.0)
                                self._fail(run_id, "review", exc)
                                failure_recorded = True
                                raise
                    else:
                        degradations.append("llm_disabled" if "llm_disabled" not in degradations else "")
                    review_payload = {"findings": [finding.to_dict() for finding in llm_findings], "degradations": [item for item in degradations if item]}
                    self.store.save_checkpoint(run_id, "review", review_payload)
            llm_findings = [Finding.from_dict(item) for item in review_payload.get("findings", [])]
            degradations = list(review_payload.get("degradations", []))

            findings = tool_findings + llm_findings
            traces = self.store.get_traces(run_id)
            result = ReviewResult(run_id=run_id, request=request, findings=findings, traces=traces, cost_usd=float(self.store.get_run(run_id)["cost_usd"]), budget_usd=config.budget_usd, degradations=degradations)
            render = self.store.get_checkpoint(run_id, "render")
            if render is None:
                current_stage = "render"
                from .report import render_markdown
                result.markdown = render_markdown(result)
                self.store.save_checkpoint(run_id, "render", {"markdown": result.markdown})
            else:
                result.markdown = render.get("markdown", "")
            self.store.update_run(run_id, status="completed")
            result.traces = self.store.get_traces(run_id)
            return result
        except Exception as exc:
            if not failure_recorded:
                self._fail(run_id, current_stage, exc)
            raise
