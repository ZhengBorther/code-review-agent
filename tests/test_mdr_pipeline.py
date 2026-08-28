import json
import sqlite3
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
    assert "规则: GO-STYLE-001" in result.markdown
    assert "严重度: warning" in result.markdown


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
    assert any(trace["kind"] == "mdr_batch" and "unknown" in trace.get("error", "") for trace in result.traces)


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
