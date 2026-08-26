# Code Review Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python CLI that reviews a local diff or GitHub/GitLab URL through a checkpointed, budget-aware, secret-safe pipeline and emits an auditable Chinese Markdown report.

**Architecture:** Use a small standard-library-first package. Typed dataclasses define change requests, findings, traces, and model responses. SQLite persists runs/checkpoints/traces. The orchestrator executes idempotent `fetch`, `sanitize`, `tools`, `review`, and `render` stages through adapter, tool registry, LLM client, and renderer interfaces.

**Tech Stack:** Python 3.11+, `argparse`, `sqlite3`, `dataclasses`, `urllib.request`, `hashlib`, `re`, `json`, `pytest`.

## Global Constraints

- Secrets must be redacted before any LLM request.
- No arbitrary repository commands are executed in v1.
- Checkpoint and trace state is persisted locally in SQLite.
- Budget degradation order is fallback model, context truncation, then no LLM.
- New tools are added through declarative `ToolSpec` registration.
- v1 outputs Markdown locally; remote publishing remains an interface only.

---

### Task 1: Package Scaffolding and Domain Models

**Files:**
- Create: `pyproject.toml`
- Create: `review_agent/__init__.py`
- Create: `review_agent/models.py`
- Create: `tests/test_models.py`

**Interfaces:**
- Produces `ChangeRequest`, `Finding`, `TraceRecord`, `LLMResponse`, `RunConfig` dataclasses and JSON serialization helpers consumed by all later tasks.

- [ ] **Step 1: Write the failing test**

```python
def test_finding_round_trips_to_json():
    finding = Finding(title="Unsafe SQL", body="Use parameters", confidence="high", evidence="rule:sql", trace_id="t1")
    assert Finding.from_dict(finding.to_dict()) == finding
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_models.py`
Expected: FAIL because `review_agent.models` does not exist.

- [ ] **Step 3: Write minimal implementation**

Implement frozen/serializable dataclasses, explicit `to_dict`/`from_dict`, confidence validation (`high` or `advisory`), and a package version. Configure pytest in `pyproject.toml`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -q tests/test_models.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml review_agent tests/test_models.py
git commit -m "feat: add review domain models"
```

### Task 2: SQLite Storage and Checkpoints

**Files:**
- Create: `review_agent/storage.py`
- Create: `tests/test_storage.py`

**Interfaces:**
- `StateStore(path).create_run(config) -> str`
- `StateStore.get_checkpoint(run_id, stage) -> dict | None`
- `StateStore.save_checkpoint(run_id, stage, payload) -> None`
- `StateStore.save_trace(trace: TraceRecord) -> None`
- `StateStore.get_run(run_id) -> dict`

- [ ] **Step 1: Write the failing test**

```python
def test_checkpoint_survives_new_store_instance(tmp_path):
    db = tmp_path / "state.db"
    store = StateStore(db)
    run_id = store.create_run(RunConfig(url="local://fixture", budget_usd=1.0))
    store.save_checkpoint(run_id, "fetch", {"diff": "ok"})
    assert StateStore(db).get_checkpoint(run_id, "fetch")["diff"] == "ok"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_storage.py::test_checkpoint_survives_new_store_instance`
Expected: FAIL because `StateStore` is undefined.

- [ ] **Step 3: Write minimal implementation**

Create tables `runs`, `checkpoints`, and `traces` with primary/unique keys, JSON payload columns, UTC timestamps, and transactional upserts. Add run cost/status update helpers.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -q tests/test_storage.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add review_agent/storage.py tests/test_storage.py
git commit -m "feat: persist runs checkpoints and traces"
```

### Task 3: Adapters, Secret Redaction, and Declarative Tools

**Files:**
- Create: `review_agent/adapters.py`
- Create: `review_agent/security.py`
- Create: `review_agent/tools.py`
- Create: `tests/test_security_tools.py`

**Interfaces:**
- `LocalDiffAdapter(diff_path).fetch(url) -> ChangeRequest`
- `redact_secrets(text) -> RedactionResult`
- `ToolRegistry.register(spec: ToolSpec)` and `.run_all(change_request, sanitized_diff) -> list[Finding]`
- Built-in `TODO`/secret-in-diff analyzer registered as `ToolSpec` values.

- [ ] **Step 1: Write the failing test**

```python
def test_redaction_hides_api_key_and_tool_registry_is_declarative(tmp_path):
    result = redact_secrets("token = 'sk-test-1234567890abcdef'")
    assert "sk-test" not in result.text
    registry = ToolRegistry()
    registry.register(ToolSpec(name="constant", description="test", runner=lambda _c, _d: [], confidence="high"))
    assert [spec.name for spec in registry.specs] == ["constant"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_security_tools.py`
Expected: FAIL because adapter/security/tool modules are missing.

- [ ] **Step 3: Write minimal implementation**

Implement URL-safe local adapter, regex-based redaction for private keys, common key/token prefixes, password assignments, and high-entropy quoted values. Return deterministic placeholders and match metadata. Keep tool execution in-process and expose only registered runners.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -q tests/test_security_tools.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add review_agent/adapters.py review_agent/security.py review_agent/tools.py tests/test_security_tools.py
git commit -m "feat: add safe adapters redaction and tool registry"
```

### Task 4: OneAPI-Compatible LLM Clients and Budget Policy

**Files:**
- Create: `review_agent/llm.py`
- Create: `review_agent/budget.py`
- Create: `tests/test_llm_budget.py`

**Interfaces:**
- `OpenAICompatibleClient(base_url, api_key, timeout).review(prompt, model) -> LLMResponse`
- `DeterministicClient.review(prompt, model) -> LLMResponse`
- `BudgetController(config).select(model, estimated_tokens) -> Decision`

- [ ] **Step 1: Write the failing test**

```python
def test_budget_selects_fallback_then_no_llm():
    controller = BudgetController(RunConfig(url="x", budget_usd=0.01, model="large", fallback_model="small"))
    assert controller.select("large", 100000).model == "small"
    assert controller.select("small", 100000).allow_llm is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_llm_budget.py`
Expected: FAIL because clients and policy are missing.

- [ ] **Step 3: Write minimal implementation**

Use `urllib.request` to POST OpenAI chat-completions JSON to `base_url`, send bearer auth, parse `choices[0].message.content` and optional `usage`. Implement deterministic offline output. Track estimated/actual cost and return explicit decisions for fallback, truncation, or disabled LLM.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -q tests/test_llm_budget.py`
Expected: PASS without network access.

- [ ] **Step 5: Commit**

```bash
git add review_agent/llm.py review_agent/budget.py tests/test_llm_budget.py
git commit -m "feat: add OneAPI client and budget degradation"
```

### Task 5: Checkpointed Pipeline and Markdown Report

**Files:**
- Create: `review_agent/pipeline.py`
- Create: `review_agent/report.py`
- Create: `tests/test_pipeline_report.py`

**Interfaces:**
- `ReviewPipeline(store, adapter, tools, llm, config).run(url_or_run_id: str) -> ReviewResult`
- `render_markdown(result) -> str`

- [ ] **Step 1: Write the failing test**

```python
def test_pipeline_resumes_and_marks_confidence(tmp_path):
    config = RunConfig(url="local://fixture", budget_usd=1.0, offline=True)
    first = ReviewPipeline(StateStore(tmp_path / "state.db"), LocalDiffAdapter(FIXTURE), ToolRegistry.with_builtins(), DeterministicClient(), config).run("local://fixture")
    resumed = ReviewPipeline(StateStore(tmp_path / "state.db"), LocalDiffAdapter(FIXTURE), ToolRegistry.with_builtins(), DeterministicClient(), config).run(first.run_id)
    markdown = render_markdown(resumed)
    assert "可直接采纳" in markdown and "trace-" in markdown
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_pipeline_report.py`
Expected: FAIL because orchestration and renderer are missing.

- [ ] **Step 3: Write minimal implementation**

Implement stable run IDs, stage checkpoint lookup, stage-level error traces, sanitizer-before-LLM ordering, tool trace creation, budget decisions, and aggregation. Render metadata, high/advisory sections, degradation notes, and a trace appendix with redacted prompt/reply and diff hash.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -q tests/test_pipeline_report.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add review_agent/pipeline.py review_agent/report.py tests/test_pipeline_report.py
git commit -m "feat: add resumable review pipeline and markdown report"
```

### Task 6: CLI, Fixtures, and End-to-End Verification

**Files:**
- Create: `review_agent/cli.py`
- Create: `review_agent/__main__.py`
- Create: `tests/fixtures/sample.diff`
- Create: `tests/test_cli_e2e.py`
- Create: `README.md`

**Interfaces:**
- Command: `python -m review_agent review --diff-file tests/fixtures/sample.diff --output report.md --state-dir .review-state --offline`

- [ ] **Step 1: Write the failing test**

```python
def test_cli_generates_report_without_network(tmp_path):
    output = tmp_path / "report.md"
    assert main(["review", "--diff-file", str(FIXTURE), "--output", str(output), "--offline"]) == 0
    assert "Code Review" in output.read_text()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_cli_e2e.py`
Expected: FAIL because the module entry point is missing.

- [ ] **Step 3: Write minimal implementation**

Wire argparse options for URL/diff, state directory, output, budget, model, fallback model, OneAPI base URL/key, and `--offline`. Select local adapter for `--diff-file`, deterministic client for offline mode, return nonzero on failed runs, and document environment variables/configuration and safety guarantees in `README.md`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -q`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add review_agent tests README.md
git commit -m "feat: expose code review agent CLI"
```

## Final Verification

- [ ] Run `pytest -q` and confirm zero failures.
- [ ] Run the documented offline CLI command and inspect the generated Markdown for redaction, confidence sections, budget metadata, and trace IDs.
- [ ] Run `python -m review_agent --help` and confirm the command exits 0.
- [ ] Run `git diff --check` and inspect `git status --short` for only intended files.
