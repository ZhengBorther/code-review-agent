from pathlib import Path

from review_agent.adapters import LocalDiffAdapter
from review_agent.llm import DeterministicClient
from review_agent.models import RunConfig
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
