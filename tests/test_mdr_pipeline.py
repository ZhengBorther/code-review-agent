import json
import sqlite3
import threading
import time
from dataclasses import replace

from review_agent.adapters import LocalDiffAdapter
from review_agent.config import RulesConfig
from review_agent.llm import LLMResponse
from review_agent.models import RunConfig
from review_agent.pipeline import ReviewPipeline
from review_agent.rules import RuleRegistry
from review_agent.storage import StateStore
from review_agent.tools import ToolRegistry
from tests.rule_fixtures import GO_DIFF, MIXED_DIFF, make_rule


class JsonRuleClient:
    def __init__(self, payload):
        self.payload = payload
        self.rule_calls = 0

    def review(self, prompt, model, **kwargs):
        if "MDR_RULE_BATCH" in prompt:
            self.rule_calls += 1
            text = json.dumps(self.payload)
        else:
            text = "generic advisory"
        return LLMResponse(text=text, model=model, cost_usd=0.0)


class LanguageCountingJsonClient(JsonRuleClient):
    def __init__(self):
        super().__init__({"findings": []})
        self.language_calls = {"go": 0, "python": 0}

    def review(self, prompt, model, **kwargs):
        for language in self.language_calls:
            if f"LANGUAGE: {language}" in prompt:
                self.language_calls[language] += 1
        return super().review(prompt, model, **kwargs)


def pipeline_with_rules(tmp_path, diff, rules, client):
    diff_file = tmp_path / "change.diff"
    diff_file.write_text(diff, encoding="utf-8")
    registry = RuleRegistry(RulesConfig())
    for rule in rules:
        registry.register(rule)
    return ReviewPipeline(
        StateStore(tmp_path / "state.db"), LocalDiffAdapter(diff_file),
        ToolRegistry(), client,
        RunConfig(url="local://rules", budget_usd=1.0, offline=True),
        rules=registry,
    )


class ConcurrentLanguageClient(JsonRuleClient):
    def __init__(self):
        super().__init__({"findings": []})
        self.active = 0
        self.peak = 0
        self.lock = threading.Lock()

    def review(self, prompt, model, **kwargs):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(0.05)
        with self.lock:
            self.active -= 1
        return super().review(prompt, model, **kwargs)


class OneLanguageFailsClient(JsonRuleClient):
    def review(self, prompt, model, **kwargs):
        if "LANGUAGE: python" in prompt:
            raise RuntimeError("python provider failure")
        return super().review(prompt, model, **kwargs)


class InvalidThenValidClient(JsonRuleClient):
    def __init__(self):
        super().__init__({"findings": [{
            "rule_id": "GO-STYLE-001", "file_path": "internal/user.go",
            "line_start": 1, "title": "参数过多", "body": "请使用结构体封装参数",
            "evidence": "函数参数超过4个",
        }]})

    def review(self, prompt, model, **kwargs):
        self.rule_calls += 1
        if self.rule_calls == 1:
            return LLMResponse(text="", model=model, cost_usd=0.0)
        return LLMResponse(text=json.dumps(self.payload), model=model, cost_usd=0.0)


def test_pipeline_runs_independent_language_batches_concurrently(tmp_path):
    client = ConcurrentLanguageClient()
    go_rule = make_rule()
    py_rule = make_rule("PY-STYLE-001", "python")
    pipeline = pipeline_with_rules(tmp_path, MIXED_DIFF, (go_rule, py_rule), client)
    pipeline.config = replace(pipeline.config, max_concurrency=2)
    result = pipeline.run("local://concurrent")
    assert client.peak == 2
    assert sum(trace["kind"] == "mdr_batch" for trace in result.traces) == 2


def test_concurrent_batches_cannot_exceed_single_pr_budget(tmp_path):
    client = ConcurrentLanguageClient()
    diff_file = tmp_path / "change.diff"
    diff_file.write_text(MIXED_DIFF, encoding="utf-8")
    registry = RuleRegistry(RulesConfig())
    registry.register(make_rule())
    registry.register(make_rule("PY-STYLE-001", "python"))
    config = RunConfig(
        url="local://budget-race", budget_usd=0.01,
        model="small", fallback_model="small", max_concurrency=2,
    )
    result = ReviewPipeline(
        StateStore(tmp_path / "state.db"), LocalDiffAdapter(diff_file),
        ToolRegistry(), client, config, rules=registry,
    ).run("local://budget-race")
    assert client.rule_calls == 1
    assert result.cost_usd <= config.budget_usd
    assert "budget_exceeded" in result.degradations


def test_same_run_cannot_be_advanced_by_two_orchestrators(tmp_path):
    client = ConcurrentLanguageClient()
    pipeline = pipeline_with_rules(tmp_path, GO_DIFF, (make_rule(),), client)
    run_id = pipeline.store.create_run(pipeline.config)
    assert pipeline.store.acquire_run_lease(run_id, "other-worker")
    try:
        try:
            pipeline.run(run_id)
        except RuntimeError as exc:
            assert "already active" in str(exc)
        else:
            raise AssertionError("second orchestrator must not enter an active run")
    finally:
        pipeline.store.release_run_lease(run_id, "other-worker")


def test_concurrent_batch_failure_marks_run_failed_and_keeps_sibling_checkpoint(tmp_path):
    client = OneLanguageFailsClient({"findings": []})
    pipeline = pipeline_with_rules(
        tmp_path, MIXED_DIFF,
        (make_rule(), make_rule("PY-STYLE-001", "python")), client,
    )
    pipeline.config = replace(pipeline.config, max_concurrency=2)
    try:
        pipeline.run("local://partial-failure")
    except RuntimeError as exc:
        assert "concurrent MDR batches failed" in str(exc)
    else:
        raise AssertionError("failed language batch must fail the run")
    run = pipeline.store.find_latest_run("local://partial-failure")
    assert run["status"] == "failed"
    assert pipeline.store.get_checkpoint(run["run_id"], "rules:go:0") is not None
    assert any(trace["kind"] == "mdr_batch" and trace["error"] for trace in pipeline.store.get_traces(run["run_id"]))

    recovery_client = LanguageCountingJsonClient()
    recovered = pipeline_with_rules(
        tmp_path, MIXED_DIFF,
        (make_rule(), make_rule("PY-STYLE-001", "python")), recovery_client,
    ).run(run["run_id"])
    assert recovered.run_id == run["run_id"]
    assert recovery_client.language_calls == {"go": 0, "python": 1}
    assert pipeline.store.get_run(run["run_id"])["status"] == "completed"


def test_pipeline_calls_llm_once_for_multiple_go_rules(tmp_path):
    client = JsonRuleClient({"findings": []})
    result = pipeline_with_rules(
        tmp_path, diff=GO_DIFF,
        rules=(make_rule(), make_rule("GO-ERROR-001")), client=client,
    ).run("local://go")
    assert client.rule_calls == 1
    assert any(trace["kind"] == "mdr_batch" for trace in result.traces)


def test_rule_checkpoint_reuses_hash_and_reruns_changed_rule(tmp_path):
    client = JsonRuleClient({"findings": []})
    go_rule = make_rule()
    first = pipeline_with_rules(tmp_path, GO_DIFF, (go_rule,), client).run("local://go")
    pipeline_with_rules(tmp_path, GO_DIFF, (go_rule,), client).run(first.run_id)
    assert client.rule_calls == 1
    changed = replace(go_rule, prompt_hint="changed requirement")
    pipeline_with_rules(tmp_path, GO_DIFF, (changed,), client).run(first.run_id)
    assert client.rule_calls == 2


def test_invalid_mdr_response_is_retryable_on_resume(tmp_path):
    client = InvalidThenValidClient()
    pipeline = pipeline_with_rules(tmp_path, GO_DIFF, (make_rule(),), client)
    first = pipeline.run("local://invalid-then-valid")
    assert client.rule_calls == 1
    assert not first.findings
    assert pipeline.store.get_checkpoint(first.run_id, "rules:go:0") is None

    resumed = pipeline_with_rules(tmp_path, GO_DIFF, (make_rule(),), client).run(first.run_id)
    assert client.rule_calls == 2
    assert resumed.findings


def test_python_rule_change_does_not_rerun_go_batch(tmp_path):
    client = LanguageCountingJsonClient()
    go_rule, py_rule = make_rule(), make_rule("PY-STYLE-001", "python")
    first = pipeline_with_rules(tmp_path, MIXED_DIFF, (go_rule, py_rule), client).run("local://mixed")
    changed_python = replace(py_rule, prompt_hint="changed Python requirement")
    pipeline_with_rules(tmp_path, MIXED_DIFF, (go_rule, changed_python), client).run(first.run_id)
    assert client.language_calls == {"go": 1, "python": 2}


def test_mdr_finding_is_advisory_and_links_batch_trace(tmp_path):
    client = JsonRuleClient({"findings": [{
        "rule_id": "GO-STYLE-001", "file_path": "internal/user.go",
        "line_start": 4, "title": "too many parameters",
        "body": "use params struct", "evidence": "five business parameters"}]})
    result = pipeline_with_rules(tmp_path, GO_DIFF, (make_rule(),), client).run("local://go")
    finding = next(item for item in result.findings if item.rule_id == "GO-STYLE-001")
    trace = next(item for item in result.traces if item["trace_id"] == finding.trace_id)
    assert finding.confidence == "advisory"
    assert trace["kind"] == "mdr_finding"
    assert trace["parent_trace_id"].startswith("trace-")
    assert trace["prompt"]
    assert trace["response"]
    assert trace["model"] == "large"
    parent = next(item for item in result.traces if item["trace_id"] == trace["parent_trace_id"])
    assert trace["prompt_tokens"] == parent["prompt_tokens"]
    assert trace["completion_tokens"] == parent["completion_tokens"]
    assert "规则: GO-STYLE-001" in result.markdown
    assert "严重度: warning" in result.markdown


def test_mdr_batch_trace_keeps_redacted_prompt_json_valid(tmp_path):
    client = JsonRuleClient({"findings": []})
    rule = make_rule(
        prompt_hint='检查 password="abcdefghijklmnopqrstuvwxyz1234567890"',
        body='禁止 logger.info("password=%s", password)',
    )
    result = pipeline_with_rules(tmp_path, GO_DIFF, (rule,), client).run("local://trace-json")
    trace = next(item for item in result.traces if item["kind"] == "mdr_batch")
    rules_line = next(line for line in trace["prompt"].splitlines() if line.startswith("RULES:"))
    payload = json.loads(rules_line.removeprefix("RULES:"))
    assert payload[0]["rule_id"] == "GO-STYLE-001"


def test_mdr_recovery_releases_unresolved_reservation(tmp_path):
    client = JsonRuleClient({"findings": []})
    pipeline = pipeline_with_rules(tmp_path, GO_DIFF, (make_rule(),), client)
    result = pipeline.run("local://go")
    store = StateStore(tmp_path / "state.db")
    store.save_checkpoint(result.run_id, "rules:go:0", {"ruleset_hash": "stale", "diff_hash": "stale", "findings": []})
    # Simulate an interrupted request with a matching in-flight reservation.
    cp = store.get_checkpoint(result.run_id, "rules:go:0:reservation")
    store.save_checkpoint(result.run_id, "rules:go:0", {"ruleset_hash": "stale", "diff_hash": "stale", "findings": []})
    store.save_checkpoint(result.run_id, "rules:go:0:reservation", {**cp, "status": "in_flight"})
    with sqlite3.connect(tmp_path / "state.db") as connection:
        connection.execute("UPDATE reservations SET status = 'in_flight' WHERE token = ?", (cp["token"],))
    resumed = pipeline_with_rules(tmp_path, GO_DIFF, (make_rule(),), client).run(result.run_id)
    assert client.rule_calls == 1
    assert "inflight_reservation_recovered" in resumed.degradations
    assert StateStore(tmp_path / "state.db").get_checkpoint(result.run_id, "rules:go:0:reservation")["status"] == "recovered"
    with sqlite3.connect(tmp_path / "state.db") as connection:
        assert connection.execute("SELECT status FROM reservations WHERE token = ?", (cp["token"],)).fetchone()[0] != "in_flight"


def test_unknown_language_emits_mdr_diagnostic_trace(tmp_path):
    diff = "diff --git a/data.xyz b/data.xyz\n--- a/data.xyz\n+++ b/data.xyz\n+value\n"
    result = pipeline_with_rules(tmp_path, diff, (make_rule(),), JsonRuleClient({"findings": []})).run("local://unknown")
    trace = next(trace for trace in result.traces if trace["kind"] == "mdr_batch" and "unknown" in trace.get("error", ""))
    assert trace["prompt"] == "未识别语言，未调用模型；原始 diff 仅用于诊断。"
    assert trace["response"] == diff
    assert trace["metadata"]["trace_role"] == "diagnostic_diff"
    assert "诊断原始 diff" in result.markdown


def test_unknown_language_runs_common_rule(tmp_path):
    diff = "diff --git a/data.xyz b/data.xyz\n--- a/data.xyz\n+++ b/data.xyz\n+value\n"
    common = make_rule("COMMON-STYLE-001", "common")
    client = JsonRuleClient({"findings": []})
    result = pipeline_with_rules(tmp_path, diff, (common,), client).run("local://unknown-common")
    assert client.rule_calls == 1
    assert not any("unknown_language" in trace.get("error", "") for trace in result.traces)


def test_pending_db_reservation_recovery_emits_trace(tmp_path):
    client = JsonRuleClient({"findings": []})
    pipeline = pipeline_with_rules(tmp_path, GO_DIFF, (make_rule(),), client)
    original = pipeline.store.save_checkpoint
    crashed = {"value": False}

    def save_checkpoint(run_id, stage, payload, **kwargs):
        if stage == "rules:go:0:reservation" and payload.get("status") == "in_flight" and not crashed["value"]:
            crashed["value"] = True
            raise RuntimeError("simulated in-flight checkpoint crash")
        return original(run_id, stage, payload, **kwargs)

    pipeline.store.save_checkpoint = save_checkpoint
    try:
        pipeline.run("local://go")
    except RuntimeError:
        pass
    run = StateStore(tmp_path / "state.db").find_latest_run("local://go")
    resumed = pipeline_with_rules(tmp_path, GO_DIFF, (make_rule(),), client).run(run["run_id"])
    assert "inflight_reservation_recovered" in resumed.degradations
    assert any(trace["kind"] == "mdr_batch" and "unresolved" in trace.get("error", "") for trace in resumed.traces)


def test_completed_reservation_rebuilds_findings_after_checkpoint_crash(tmp_path):
    payload = {"findings": [{"rule_id": "GO-STYLE-001", "file_path": "internal/user.go", "line_start": 4,
                              "title": "too many parameters", "body": "use params struct", "evidence": "five"}]}
    client = JsonRuleClient(payload)
    pipeline = pipeline_with_rules(tmp_path, GO_DIFF, (make_rule(),), client)
    original = pipeline.store.save_checkpoint
    crashed = {"value": False}

    def save_checkpoint(run_id, stage, payload, **kwargs):
        if stage == "rules:go:0" and payload.get("findings") and not crashed["value"]:
            crashed["value"] = True
            raise RuntimeError("simulated checkpoint crash")
        return original(run_id, stage, payload, **kwargs)

    pipeline.store.save_checkpoint = save_checkpoint
    try:
        pipeline.run("local://go")
    except RuntimeError:
        pass
    run = StateStore(tmp_path / "state.db").find_latest_run("local://go")
    resumed = pipeline_with_rules(tmp_path, GO_DIFF, (make_rule(),), client).run(run["run_id"])
    assert any(item.rule_id == "GO-STYLE-001" for item in resumed.findings)


def test_orphan_reservation_is_reclaimed_after_timeout(tmp_path):
    client = JsonRuleClient({"findings": []})
    pipeline = pipeline_with_rules(tmp_path, GO_DIFF, (make_rule(),), client)
    original = pipeline.store.save_checkpoint
    crashed = {"value": False}

    def save_checkpoint(run_id, stage, payload, **kwargs):
        if stage == "rules:go:0:reservation" and payload.get("status") == "in_flight" and not crashed["value"]:
            crashed["value"] = True
            raise RuntimeError("simulated orphan checkpoint crash")
        return original(run_id, stage, payload, **kwargs)

    pipeline.store.save_checkpoint = save_checkpoint
    try:
        pipeline.run("local://go")
    except RuntimeError:
        pass
    run = StateStore(tmp_path / "state.db").find_latest_run("local://go")
    resumed = pipeline_with_rules(tmp_path, GO_DIFF, (make_rule(),), client).run(run["run_id"])
    assert client.rule_calls == 0
    with sqlite3.connect(tmp_path / "state.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM reservations WHERE status = 'in_flight'").fetchone()[0] == 0


def test_pending_reservation_without_db_row_is_retryable(tmp_path):
    client = JsonRuleClient({"findings": []})
    pipeline = pipeline_with_rules(tmp_path, GO_DIFF, (make_rule(),), client)
    original = pipeline.store.save_checkpoint
    crashed = {"value": False}

    def save_checkpoint(run_id, stage, payload, **kwargs):
        if stage == "rules:go:0:reservation" and payload.get("status") == "pending" and not crashed["value"]:
            crashed["value"] = True
            raise RuntimeError("simulated pending crash")
        return original(run_id, stage, payload, **kwargs)

    pipeline.store.save_checkpoint = save_checkpoint
    try:
        pipeline.run("local://go")
    except RuntimeError:
        pass
    run = StateStore(tmp_path / "state.db").find_latest_run("local://go")
    resumed = pipeline_with_rules(tmp_path, GO_DIFF, (make_rule(),), client).run(run["run_id"])
    assert client.rule_calls == 1
    assert not resumed.degradations


def test_removed_rule_regenerates_report_without_old_rule_trace(tmp_path):
    payload = {"findings": [{"rule_id": "GO-STYLE-001", "file_path": "internal/user.go", "line_start": 4,
                              "title": "too many parameters", "body": "use params struct", "evidence": "five"}]}
    client = JsonRuleClient(payload)
    first = pipeline_with_rules(tmp_path, GO_DIFF, (make_rule(),), client).run("local://go-remove")
    assert "GO-STYLE-001" in first.markdown
    second = pipeline_with_rules(tmp_path, GO_DIFF, (), client).run(first.run_id)
    assert "GO-STYLE-001" not in second.markdown
    assert "mdr_finding" not in second.markdown


def test_superseding_rule_checkpoint_releases_inflight_reservation(tmp_path):
    client = JsonRuleClient({"findings": []})
    pipeline = pipeline_with_rules(tmp_path, GO_DIFF, (make_rule(),), client)
    first = pipeline.run("local://supersede")
    store = StateStore(tmp_path / "state.db")
    token = "stale-token"
    assert store.reserve_budget(first.run_id, token, 0.25)
    store.save_checkpoint(first.run_id, "rules:go:99:reservation", {
        "ruleset_hash": "old", "diff_hash": "old", "status": "in_flight",
        "token": token, "reserved_usd": 0.25,
    })
    # New registry has no effective rule, so the old stage is superseded.
    resumed = pipeline_with_rules(tmp_path, GO_DIFF, (), client).run(first.run_id)
    assert resumed.run_id == first.run_id
    assert store.get_reservation(first.run_id, token)["status"] != "in_flight"


def test_changed_ruleset_same_batch_releases_old_reservation(tmp_path):
    client = JsonRuleClient({"findings": []})
    first_pipeline = pipeline_with_rules(tmp_path, GO_DIFF, (make_rule(),), client)
    first = first_pipeline.run("local://same-batch-supersede")
    store = StateStore(tmp_path / "state.db")
    token = "same-batch-stale"
    assert store.reserve_budget(first.run_id, token, 0.2)
    old_hash = first_pipeline.rules.ruleset_hash("go")
    store.save_checkpoint(first.run_id, "rules:go:0:reservation", {
        "ruleset_hash": old_hash, "diff_hash": "old-diff", "status": "in_flight",
        "token": token, "reserved_usd": 0.2,
    })
    changed = replace(make_rule(), prompt_hint="changed")
    pipeline_with_rules(tmp_path, GO_DIFF, (changed,), client).run(first.run_id)
    assert store.get_reservation(first.run_id, token)["status"] != "in_flight"


def test_completed_reservation_recovery_rebuilds_findings_when_trace_write_crashes(tmp_path):
    payload = {"findings": [{"rule_id": "GO-STYLE-001", "file_path": "internal/user.go", "line_start": 1,
                              "title": "too many parameters", "body": "use params struct", "evidence": "five"}]}
    client = JsonRuleClient(payload)
    pipeline = pipeline_with_rules(tmp_path, GO_DIFF, (make_rule(),), client)
    original = pipeline.store.save_trace
    crashed = {"value": False}

    def save_trace(trace):
        if trace.kind == "mdr_batch" and not crashed["value"]:
            crashed["value"] = True
            raise RuntimeError("simulated trace crash after settlement")
        return original(trace)

    pipeline.store.save_trace = save_trace
    try:
        pipeline.run("local://trace-crash")
    except RuntimeError:
        pass
    run = StateStore(tmp_path / "state.db").find_latest_run("local://trace-crash")
    resumed = pipeline_with_rules(tmp_path, GO_DIFF, (make_rule(),), client).run(run["run_id"])
    assert client.rule_calls == 1
    assert any(item.rule_id == "GO-STYLE-001" for item in resumed.findings)


def test_unknown_diagnostic_trace_changes_render_identity(tmp_path):
    diff = "diff --git a/data.xyz b/data.xyz\n--- a/data.xyz\n+++ b/data.xyz\n+value\n"
    client = JsonRuleClient({"findings": []})
    first = pipeline_with_rules(tmp_path, diff, (), client).run("local://unknown-render")
    second = pipeline_with_rules(tmp_path, diff, (), client).run(first.run_id)
    first_unknown = [t["trace_id"] for t in first.traces if t.get("error") == "unknown_language"]
    second_unknown = [t["trace_id"] for t in second.traces if t.get("error") == "unknown_language"]
    assert first_unknown and second_unknown and first_unknown[-1] != second_unknown[-1]
    assert second_unknown[-1] in second.markdown
