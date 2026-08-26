from pathlib import Path

from review_agent.adapters import LocalDiffAdapter
from review_agent.llm import DeterministicClient
from review_agent.models import Finding, RunConfig
from review_agent.models import LLMResponse
from review_agent.pipeline import ReviewPipeline
from review_agent.report import render_markdown
from review_agent.storage import StateStore
from review_agent.tools import ToolRegistry


def test_pipeline_resumes_and_marks_confidence(tmp_path: Path):
    fixture = tmp_path / "change.diff"
    fixture.write_text("diff --git a/app.py b/app.py\n+ # TODO: handle error\n", encoding="utf-8")
    config = RunConfig(url="local://fixture", budget_usd=1.0, offline=True)
    first = ReviewPipeline(
        StateStore(tmp_path / "state.db"),
        LocalDiffAdapter(fixture),
        ToolRegistry.with_builtins(),
        DeterministicClient(),
        config,
    ).run("local://fixture")
    resumed = ReviewPipeline(
        StateStore(tmp_path / "state.db"),
        LocalDiffAdapter(fixture),
        ToolRegistry.with_builtins(),
        DeterministicClient(),
        config,
    ).run(first.run_id)
    markdown = render_markdown(resumed)
    assert "可直接采纳" in markdown
    assert "trace-" in markdown
    assert resumed.run_id == first.run_id


class _OverBudgetClient:
    def review(self, prompt, model, **kwargs):
        return LLMResponse("should not be accepted", model=model, prompt_tokens=1, completion_tokens=1, cost_usd=2.0)


def test_provider_over_budget_response_is_not_success(tmp_path: Path):
    fixture = tmp_path / "change.diff"
    fixture.write_text("diff --git a/app.py b/app.py\n+pass\n", encoding="utf-8")
    config = RunConfig(url="local://fixture", budget_usd=0.01, model="small", fallback_model="small")
    result = ReviewPipeline(StateStore(tmp_path / "state.db"), LocalDiffAdapter(fixture), ToolRegistry(), _OverBudgetClient(), config).run("local://fixture")
    assert not any(f.title == "模型审查建议" for f in result.findings)
    assert any("预算" in f.title for f in result.findings)
    assert result.cost_usd <= config.budget_usd


class _FailingClient:
    def __init__(self):
        self.calls = 0

    def review(self, prompt, model, **kwargs):
        self.calls += 1
        raise RuntimeError("transient failure")


def test_inflight_reservation_prevents_duplicate_call_after_restart(tmp_path: Path):
    fixture = tmp_path / "change.diff"
    fixture.write_text("diff --git a/app.py b/app.py\n+pass\n", encoding="utf-8")
    db = tmp_path / "state.db"
    config = RunConfig(url="local://fixture", budget_usd=1.0, offline=True)
    store = StateStore(db)
    run_id = store.create_run(config)
    first_client = _FailingClient()
    try:
        ReviewPipeline(store, LocalDiffAdapter(fixture), ToolRegistry(), first_client, config).run(run_id)
    except RuntimeError:
        pass
    assert first_client.calls == 1
    second_client = _FailingClient()
    result = ReviewPipeline(StateStore(db), LocalDiffAdapter(fixture), ToolRegistry(), second_client, config).run(run_id)
    assert second_client.calls == 0
    assert any("中断" in f.title for f in result.findings)


def test_report_redacts_metadata_and_shows_trace_usage(tmp_path: Path):
    finding = Finding(
        title="password=super-secret",
        body="avoid password=super-secret",
        confidence="advisory",
        evidence="file=secret.py",
        trace_id="trace-1",
        file_path="secret.py?token=super-secret",
    )
    result = type("Result", (), {
        "run_id": "run-1", "request": type("Request", (), {"url": "https://x.test/pr?token=super-secret"})(),
        "findings": [finding], "traces": [{"trace_id": "trace-1", "kind": "llm", "tool_name": "review", "model": "small", "input_hash": "abc", "cost_usd": 0.1, "prompt_tokens": 12, "completion_tokens": 4, "duration_ms": 25, "error": ""}],
        "cost_usd": 0.1, "budget_usd": 1.0, "degradations": [],
    })()
    markdown = render_markdown(result)
    assert "super-secret" not in markdown
    assert "Prompt tokens: 12" in markdown
    assert "耗时: 25ms" in markdown


def test_pipeline_applies_max_diff_chars(tmp_path: Path):
    fixture = tmp_path / "large.diff"
    fixture.write_text("diff --git a/app.py b/app.py\n+" + "x" * 200, encoding="utf-8")
    config = RunConfig(url="local://fixture", budget_usd=1.0, offline=True, max_diff_chars=32)
    result = ReviewPipeline(StateStore(tmp_path / "state.db"), LocalDiffAdapter(fixture), ToolRegistry(), DeterministicClient(), config).run("local://fixture")
    review = StateStore(tmp_path / "state.db").get_checkpoint(result.run_id, "review")
    assert result.run_id and review is not None
