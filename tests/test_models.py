import pytest

from review_agent.models import ChangeRequest, Finding, LLMResponse, RunConfig, TraceRecord


def test_finding_round_trips_to_json():
    finding = Finding(title="Unsafe SQL", body="Use parameters", confidence="high", evidence="rule:sql", trace_id="t1")
    assert Finding.from_dict(finding.to_dict()) == finding


def test_domain_models_round_trip_to_dict():
    request = ChangeRequest(url="local://fixture", title="Example", author="alice", diff="diff --git a/a.py b/a.py")
    response = LLMResponse(text="Looks good", model="small", prompt_tokens=10, completion_tokens=4, cost_usd=0.001)
    trace = TraceRecord(trace_id="trace-1", run_id="run-1", kind="llm", input_hash="abc", prompt="safe", response="ok", model="small", cost_usd=0.001)
    config = RunConfig(url=request.url, budget_usd=1.0, model="large", fallback_model="small", offline=True)
    for cls, value in ((ChangeRequest, request), (LLMResponse, response), (TraceRecord, trace), (RunConfig, config)):
        assert cls.from_dict(value.to_dict()) == value


def test_finding_rejects_unknown_confidence():
    with pytest.raises(ValueError):
        Finding(title="x", body="y", confidence="maybe", evidence="z")
