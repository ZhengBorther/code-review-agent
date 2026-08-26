import json

from review_agent.models import RunConfig, TraceRecord
from review_agent.storage import StateStore


def test_checkpoint_survives_new_store_instance(tmp_path):
    db = tmp_path / "state.db"
    store = StateStore(db)
    run_id = store.create_run(RunConfig(url="local://fixture", budget_usd=1.0))
    store.save_checkpoint(run_id, "fetch", {"diff": "ok"})
    assert StateStore(db).get_checkpoint(run_id, "fetch")["diff"] == "ok"


def test_create_run_persists_config_and_defaults(tmp_path):
    store = StateStore(tmp_path / "state.db")
    config = RunConfig(url="local://fixture", budget_usd=2.5, offline=True)
    run_id = store.create_run(config)
    run = store.get_run(run_id)
    assert run["run_id"] == run_id
    assert run["url"] == config.url
    assert run["config"] == config.to_dict()
    assert run["status"] == "running"
    assert run["cost_usd"] == 0.0
    assert run["budget_usd"] == 2.5


def test_checkpoint_upsert_replaces_payload(tmp_path):
    store = StateStore(tmp_path / "state.db")
    run_id = store.create_run(RunConfig(url="local://fixture"))
    store.save_checkpoint(run_id, "fetch", {"version": 1})
    store.save_checkpoint(run_id, "fetch", {"version": 2})
    assert store.get_checkpoint(run_id, "fetch") == {"version": 2}


def test_failed_checkpoint_is_not_treated_as_completed(tmp_path):
    store = StateStore(tmp_path / "state.db")
    run_id = store.create_run(RunConfig(url="local://fixture"))
    store.save_checkpoint(run_id, "fetch", {"error": "network"}, status="failed")

    assert store.get_checkpoint(run_id, "fetch") is None
    assert store.get_checkpoint_record(run_id, "fetch")["status"] == "failed"


def test_trace_persists_all_audit_fields_and_updates_cost(tmp_path):
    store = StateStore(tmp_path / "state.db")
    run_id = store.create_run(RunConfig(url="local://fixture"))
    trace = TraceRecord(
        trace_id="trace-1", run_id=run_id, kind="llm", input_hash="abc",
        prompt="safe prompt", response="reply", model="small",
        prompt_tokens=10, completion_tokens=4, cost_usd=0.001,
        duration_ms=12, tool_name="review", error="",
    )
    store.save_trace(trace)
    store.update_run_cost(run_id, trace.cost_usd)
    assert store.get_traces(run_id) == [trace.to_dict()]
    assert store.get_run(run_id)["cost_usd"] == 0.001


def test_run_status_can_be_updated(tmp_path):
    store = StateStore(tmp_path / "state.db")
    run_id = store.create_run(RunConfig(url="local://fixture"))
    store.update_run(run_id, status="completed")
    assert store.get_run(run_id)["status"] == "completed"


def test_budget_reservation_is_atomic_and_settled(tmp_path):
    db = tmp_path / "state.db"
    store = StateStore(db)
    run_id = store.create_run(RunConfig(url="local://fixture", budget_usd=1.0))
    assert store.reserve_budget(run_id, "r1", 0.75)
    assert not store.reserve_budget(run_id, "r2", 0.30)
    assert store.settle_reservation(run_id, "r1", 0.50)
    assert store.get_run(run_id)["cost_usd"] == 0.5
    assert store.reserve_budget(run_id, "r3", 0.5)
