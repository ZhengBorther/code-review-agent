# cr-agent Patterns Incremental Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Incrementally adopt useful `cr-agent` patterns while keeping this project MDR-only, secret-safe, SQLite-recoverable, and free of repository shell execution.

**Architecture:** Add Pydantic schemas around the existing MDR batch parser, introduce a LangGraph-backed orchestration adapter with a fallback to the existing pipeline, and provide an async batch scheduler whose each task reserves budget through SQLite before calling the provider. Add opt-in repository profiles and offline eval fixtures without changing the core stage contracts.

**Tech Stack:** Python 3.11+, existing `sqlite3`/`asyncio`/`tomllib`/`pytest`, `pydantic>=2,<3`, `langgraph>=0.2,<2`.

## Global Constraints

- Review mode is strictly `mdr_only`; no free-form generic LLM review is permitted.
- MDR files and profile files are data only; never execute code, shell, templates, or imports from them.
- All rules and diffs are secret-redacted before provider calls and persisted prompt traces.
- Every MDR batch reserves budget atomically in SQLite before provider calls.
- Every accepted MDR finding has `confidence="advisory"`, severity from MDR, and a parent batch trace.
- Existing `ReviewPipeline` callers and no-rule behavior remain compatible.
- `cr-agent` ShellTool is not copied or exposed.

---

### Task 1: Pydantic MDR Response Schemas

**Files:**
- Modify: `pyproject.toml`
- Modify: `review_agent/rule_review.py`
- Create: `tests/test_rule_schema.py`

**Interfaces:**
- `MdrFindingPayload` with bounded `rule_id`, `file_path`, `line_start`, `line_end`, `title`, `body`, `evidence`.
- `MdrResponsePayload(findings: list[MdrFindingPayload])` with maximum 1000 findings.
- `parse_rule_response(text, batch)` continues returning `RuleParseResult` and uses Pydantic validation before batch-specific checks.

- [ ] Write a failing test for Pydantic type/size validation.
- [ ] Run `pytest -q tests/test_rule_schema.py` and confirm RED.
- [ ] Add `pydantic>=2,<3` and implement models with `ConfigDict(extra="forbid")`, `Field(max_length=20000)`, positive optional line numbers, and `line_end >= line_start` validation.
- [ ] Keep file-level findings valid when both line fields are `null`; keep unknown rule/file rejection in the existing parser.
- [ ] Run `pytest -q tests/test_rule_schema.py tests/test_rule_review.py` and confirm PASS.
- [ ] Commit `feat: validate MDR responses with Pydantic`.

### Task 2: Explicit Review State and LangGraph Adapter

**Files:**
- Create: `review_agent/graph_pipeline.py`
- Modify: `review_agent/cli.py`
- Create: `tests/test_graph_pipeline.py`

**Interfaces:**
- `ReviewState(TypedDict)` fields: `run_id`, `url`, `request`, `sanitized_request`, `findings`, `degradations`, `trace_ids`, `markdown`.
- `build_review_graph(pipeline: ReviewPipeline) -> ReviewGraphAdapter`.
- `ReviewGraphAdapter.run(url_or_run_id) -> ReviewResult`.

- [ ] Write a failing test asserting graph nodes expose `fetch`, `sanitize`, `tools`, `mdr`, and `render` in order.
- [ ] Run `pytest -q tests/test_graph_pipeline.py` and confirm RED.
- [ ] Add optional LangGraph import. When installed, build a `StateGraph` with nodes that call existing pipeline stage helpers; compile without replacing SQLite checkpoints. When unavailable, return an adapter that delegates to `ReviewPipeline.run`.
- [ ] Update CLI to select the graph adapter by default while preserving identical `ReviewResult` and `mdr_only` behavior.
- [ ] Run the graph test and full regression tests.
- [ ] Commit `feat: add LangGraph orchestration adapter`.

### Task 3: Async MDR Batch Scheduler with SQLite Reservations

**Files:**
- Create: `review_agent/async_batches.py`
- Modify: `review_agent/pipeline.py`
- Modify: `review_agent/config.py`
- Modify: `review_agent/models.py`
- Create: `tests/test_async_batches.py`

**Interfaces:**
- `AsyncBatchConfig(max_concurrency: int = 2)`.
- `async run_mdr_batches_async(*, batches, reviewer, store, run_id, config) -> BatchRunResult`.
- `BatchRunResult(findings, degradations, trace_ids)`.

- [ ] Write a failing test with two independent language batches and a client that records overlapping start times.
- [ ] Run `pytest -q tests/test_async_batches.py` and confirm RED.
- [ ] Add `max_concurrency` to `RunConfig`/`AppConfig` and TOML `[review].max_concurrency`, validating values from 1 to 16.
- [ ] Implement an asyncio semaphore around batch tasks. Each task must use the existing `pending -> reserve_budget -> in_flight -> settle_reservation` protocol and existing `RuleBatchReviewer`/parser contracts.
- [ ] Ensure a rejected reservation returns a deterministic batch trace and does not call the provider; ensure one batch failure does not erase successful sibling checkpoints.
- [ ] Integrate async scheduler into MDR stage only; generic review remains absent.
- [ ] Run focused and full tests.
- [ ] Commit `feat: schedule independent MDR batches asynchronously`.

### Task 4: Repository Profiles and Offline Eval Cases

**Files:**
- Modify: `review_agent/config.py`
- Create: `review_agent/profiles.py`
- Modify: `review_agent/cli.py`
- Create: `eval/cases/GO-STYLE-001/many-parameters/patch.diff`
- Create: `eval/cases/GO-STYLE-001/many-parameters/expect.json`
- Create: `tests/test_profiles_eval.py`

**Interfaces:**
- `RepoProfile(name, repo, rules_dirs, enabled_languages, skip_globs)`.
- `load_profiles(path) -> tuple[RepoProfile, ...]`.
- `select_profile(profiles, repo_path) -> RepoProfile | None`.
- CLI option `--profile PATH`; profile only filters files/rules and never executes commands.

- [ ] Write failing profile selection and skip-glob tests.
- [ ] Run focused tests and confirm RED.
- [ ] Implement TOML `[[profiles]]` parsing with exact path matching and `fnmatch` skip filters.
- [ ] Apply profile filters before MDR batch construction and include profile hash in ruleset identity.
- [ ] Add an eval runner/test that executes the fixture through the deterministic MDR client and asserts expected rule ID, advisory confidence, and zero shell/network calls.
- [ ] Run focused and full tests.
- [ ] Commit `feat: add repository profiles and MDR eval fixture`.

### Task 5: Documentation and Final Verification

**Files:**
- Modify: `README.md`
- Modify: `review-agent.example.toml`
- Modify: `docs/superpowers/specs/2026-08-28-mdr-rule-plugins-design.md`
- Create: `docs/eval.md`

**Interfaces:**
- Document strict MDR-only mode, graph fallback, async batch limits, profile syntax, eval command, budget reservation behavior, and shell prohibition.

- [ ] Add a complete config example including `mode="mdr_only"`, `max_concurrency`, model pricing, and profile reference.
- [ ] Document Pydantic JSON response schema and file-level finding behavior.
- [ ] Document that LangGraph is optional and SQLite remains the recovery source of truth.
- [ ] Document the eval fixture command and expected output.
- [ ] Run `pytest -q`, `python3 -m review_agent --help`, the offline MDR example, and `git diff --check`.
- [ ] Commit `docs: document incremental cr-agent patterns`.

## Final Verification

- [ ] `pytest -q` passes with zero failures.
- [ ] Default CLI performs only MDR review and emits no generic review finding.
- [ ] Two language batches can overlap while SQLite reservations remain within budget.
- [ ] Restart/recovery tests show no duplicate provider call for a completed or in-flight batch.
- [ ] Profile and MDR code blocks are never executed.
- [ ] `git diff --check` passes and only intended files are changed.
