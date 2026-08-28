# MDR Rule Plugins Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add configuration-driven `.mdr` Code Review rules that are loaded from authorized directories, grouped by diff language, evaluated in budgeted LLM batches, and rendered with rule-level traces.

**Architecture:** Parse YAML front matter into immutable `ReviewRule` objects, register and filter them through `RuleRegistry`, and split sanitized unified diffs by language. A `RuleBatchReviewer` builds strict JSON prompts and validates model output. `ReviewPipeline` adds a generic rule-review stage with per-language checkpoint keys, while the existing generic review, budget, secret redaction, and Markdown report remain reusable.

**Tech Stack:** Python 3.11+, `tomllib`, `PyYAML>=6,<7`, existing `sqlite3`/`argparse`/`urllib`, and `pytest`.

## Global Constraints

- MDR files are data only; never execute code blocks, imports, templates, or commands found in rules.
- Rules are loaded only from the default user directory or directories explicitly authorized through TOML/CLI.
- Rules and diffs are secret-redacted before any LLM request or persisted prompt trace.
- Rules are grouped by language; multiple applicable rules share a model request unless the batch exceeds its configured size.
- All pure-LLM MDR findings have `confidence="advisory"`; severity and confidence remain independent.
- Every accepted MDR finding has a finding trace that points to a batch trace.
- Rule/checkpoint identity includes normalized ruleset and relevant-language diff hashes.
- Existing deterministic tools and generic LLM review keep working when no MDR rules are configured.

---

### Task 1: Rule Models, TOML Configuration, and Safe MDR Loading

**Files:**
- Modify: `pyproject.toml`
- Create: `review_agent/rules.py`
- Create: `review_agent/config.py`
- Create: `tests/rule_fixtures.py`
- Create: `tests/test_rule_loading.py`

**Interfaces:**
- `ReviewRule(id, title, language, domains, severity, prompt_hint, deprecated, body, source)`
- `RulesConfig(directories, enabled_languages, disabled_rules)`
- `load_rules_config(path, cli_directories) -> RulesConfig`
- `MdrRuleLoader(max_file_bytes=262_144).load(directory) -> list[ReviewRule]`

Create `tests/rule_fixtures.py` with `VALID_GO_RULE` containing the complete approved `GO-STYLE-001` MDR and this reusable constructor after `ReviewRule` exists:

```python
def make_rule(rule_id="GO-STYLE-001", language="go", deprecated=False, **changes):
    values = dict(
        id=rule_id, title="Rule title", language=language,
        domains=("STYLE",), severity="warning",
        prompt_hint="Check the changed code", deprecated=deprecated,
        body="# Rule body", source=f"{rule_id}.mdr",
    )
    values.update(changes)
    return ReviewRule(**values)

GO_DIFF = (
    "diff --git a/internal/user.go b/internal/user.go\n"
    "--- a/internal/user.go\n+++ b/internal/user.go\n"
    "+func CreateUser(name string, age int, role string, active bool, region string) {}\n"
)
PY_DIFF = (
    "diff --git a/service.py b/service.py\n"
    "--- a/service.py\n+++ b/service.py\n+def create_user(): pass\n"
)
MIXED_DIFF = GO_DIFF + PY_DIFF
```

- [ ] **Step 1: Write a failing valid-MDR/config test**

```python
def test_loads_valid_mdr_and_merges_toml_with_cli_directories(tmp_path):
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "GO-STYLE-001.mdr").write_text(VALID_GO_RULE, encoding="utf-8")
    config_file = tmp_path / "review-agent.toml"
    config_file.write_text(
        '[rules]\ndirectories = ["rules"]\nenabled_languages = ["go"]\n'
        'disabled_rules = ["GO-OLD-001"]\n', encoding="utf-8")

    config = load_rules_config(config_file, [tmp_path / "extra"])
    loaded = MdrRuleLoader().load(config.directories[0])

    assert config.directories == (rules.resolve(), (tmp_path / "extra").resolve())
    assert loaded[0].id == "GO-STYLE-001"
    assert loaded[0].language == "go"
    assert loaded[0].source == "GO-STYLE-001.mdr"
```

- [ ] **Step 2: Run RED test**

Run: `pytest -q tests/test_rule_loading.py::test_loads_valid_mdr_and_merges_toml_with_cli_directories`

Expected: FAIL because `review_agent.rules` and `review_agent.config` do not exist.

- [ ] **Step 3: Add YAML dependency and models**

Add `dependencies = ["PyYAML>=6,<7"]` to `pyproject.toml`. Define frozen dataclasses with exact fields from the interfaces. Resolve TOML-relative directories against the TOML parent and CLI directories against the process working directory.

- [ ] **Step 4: Implement safe loader and validation tests**

```python
@pytest.mark.parametrize("content,error", [
    ("---\nid: bad id\n---\n", "invalid rule id"),
    (VALID_GO_RULE.replace("severity: warning", "severity: critical"), "invalid severity"),
    ("---\n!!python/object:os.system ['echo unsafe']\n---\n", "invalid YAML"),
])
def test_rejects_invalid_mdr(content, error, tmp_path):
    (tmp_path / "bad.mdr").write_text(content, encoding="utf-8")
    with pytest.raises(RuleLoadError, match=error):
        MdrRuleLoader().load(tmp_path)
```

Use `yaml.safe_load` only. Require all specified fields; normalize language to lowercase and domains to uppercase; validate ID with `^[A-Z][A-Z0-9_-]+$`; accept only `error`, `warning`, `info`; require boolean `deprecated`; enforce UTF-8 and size limit. Store a safe path relative to the authorized directory.

- [ ] **Step 5: Run tests and commit**

Run: `pytest -q tests/test_rule_loading.py`

Expected: PASS, including invalid UTF-8, missing fields, wrong field types, unsafe YAML, and oversized files.

```bash
git add pyproject.toml review_agent/rules.py review_agent/config.py tests/rule_fixtures.py tests/test_rule_loading.py
git commit -m "feat: load safe MDR rule configuration"
```

### Task 2: Rule Registry and Unified Diff Language Grouping

**Files:**
- Modify: `review_agent/rules.py`
- Create: `review_agent/diff_languages.py`
- Create: `tests/test_rule_registry_languages.py`

**Interfaces:**
- `RuleRegistry(config).register(rule)`
- `RuleRegistry.applicable(language) -> tuple[ReviewRule, ...]`
- `RuleRegistry.ruleset_hash(language) -> str`
- `split_diff_by_language(diff) -> tuple[LanguageDiff, ...]`
- `LanguageDiff(language, files, diff, diff_hash)`

- [ ] **Step 1: Write failing registry test**

```python
def test_registry_filters_disabled_deprecated_and_merges_common_rules():
    registry = RuleRegistry(RulesConfig(
        enabled_languages=("go",),
        disabled_rules=frozenset({"GO-OFF-001"})))
    registry.register(make_rule("COMMON-SEC-001", "common"))
    registry.register(make_rule("GO-STYLE-001", "go"))
    registry.register(make_rule("GO-OFF-001", "go"))
    registry.register(make_rule("GO-OLD-001", "go", deprecated=True))
    assert [item.id for item in registry.applicable("go")] == [
        "COMMON-SEC-001", "GO-STYLE-001"]
```

- [ ] **Step 2: Verify RED, then implement registry**

Run: `pytest -q tests/test_rule_registry_languages.py::test_registry_filters_disabled_deprecated_and_merges_common_rules`

Reject duplicate IDs across directories. Sort active rules by ID. Compute `ruleset_hash` from canonical JSON of effective rule fields and active config using sorted keys, UTF-8 and SHA-256.

- [ ] **Step 3: Write failing language split test**

```python
def test_splits_unified_diff_by_language_and_preserves_sections():
    mixed_diff = (
        "diff --git a/internal/user.go b/internal/user.go\n"
        "--- a/internal/user.go\n+++ b/internal/user.go\n+func CreateUser() {}\n"
        "diff --git a/service.py b/service.py\n"
        "--- a/service.py\n+++ b/service.py\n+def create_user(): pass\n"
    )
    grouped = {item.language: item for item in split_diff_by_language(mixed_diff)}
    assert grouped["go"].files == ("internal/user.go",)
    assert "+func CreateUser" in grouped["go"].diff
    assert grouped["python"].files == ("service.py",)
    assert "service.py" not in grouped["go"].diff
```

- [ ] **Step 4: Implement language grouping**

Use mappings for `.go`, `.py`, `.ts`, `.tsx`, `.js`, `.jsx`, `.java`, `.rs`, and `.cs`. Split on `diff --git`, prefer `+++ b/path`, skip `/dev/null`, preserve each full file section, group stable-sorted sections and SHA-256 hash exact grouped diff.

- [ ] **Step 5: Run and commit**

Run: `pytest -q tests/test_rule_registry_languages.py`

```bash
git add review_agent/rules.py review_agent/diff_languages.py tests/test_rule_registry_languages.py
git commit -m "feat: group MDR rules and diffs by language"
```

### Task 3: Rule Batches and Strict Structured Response Parsing

**Files:**
- Modify: `review_agent/models.py`
- Create: `review_agent/rule_review.py`
- Create: `tests/test_rule_review.py`

**Interfaces:**
- Extend `Finding` with `severity: str = ""`, `rule_id: str = ""`.
- Extend `TraceRecord` with `parent_trace_id`, `rule_id`, `ruleset_hash` defaulting to empty strings and `metadata: dict[str, Any]` using an empty default factory.
- `build_rule_batches(language_diff, rules, max_prompt_chars) -> tuple[RuleBatch, ...]`
- `build_rule_prompt(batch) -> str`
- `parse_rule_response(text, batch) -> RuleParseResult`

- [ ] **Step 1: Write failing batching/redaction test**

```python
def test_multiple_go_rules_share_one_batch_and_prompt_is_redacted():
    language_diff = LanguageDiff(
        language="go", files=("internal/user.go",),
        diff=GO_DIFF + "+token = 'sk-test-secret-123456789'\n",
        diff_hash="hash-go")
    batches = build_rule_batches(
        language_diff,
        (make_rule(), make_rule("COMMON-SECURITY-001", "common")),
        20_000)
    prompt = build_rule_prompt(batches[0])
    assert len(batches) == 1
    assert "GO-STYLE-001" in prompt and "COMMON-SECURITY-001" in prompt
    assert "sk-test-secret" not in prompt and "[REDACTED:" in prompt
```

- [ ] **Step 2: Verify RED and implement stable batching**

Run: `pytest -q tests/test_rule_review.py::test_multiple_go_rules_share_one_batch_and_prompt_is_redacted`

Prompt begins with `MDR_RULE_BATCH` and `LANGUAGE: <normalized-language>`, then includes the JSON-only contract plus rule ID, title, severity, domains, hint, body and language diff. Redact the complete constructed prompt. If oversized, split by stable rule ID without mixing languages.

- [ ] **Step 3: Write failing strict parser tests**

```python
def test_parser_accepts_known_rule_and_forces_advisory():
    go_batch = build_rule_batches(
        LanguageDiff("go", ("internal/user.go",), GO_DIFF, "hash-go"),
        (make_rule(),), 20_000)[0]
    parsed = parse_rule_response(json.dumps({"findings": [{
        "rule_id": "GO-STYLE-001", "file_path": "internal/user.go",
        "line_start": 12, "title": "too many parameters",
        "body": "use CreateUserParams", "evidence": "five parameters",
        "confidence": "high"}]}), go_batch)
    assert parsed.findings[0].confidence == "advisory"
    assert parsed.findings[0].severity == "warning"
    assert parsed.findings[0].rule_id == "GO-STYLE-001"

def test_parser_rejects_unknown_rule_and_file():
    go_batch = build_rule_batches(
        LanguageDiff("go", ("internal/user.go",), GO_DIFF, "hash-go"),
        (make_rule(),), 20_000)[0]
    unknown = json.dumps({"findings": [
        {"rule_id": "GO-UNKNOWN-001", "file_path": "internal/user.go",
         "line_start": 1, "title": "x", "body": "x", "evidence": "x"},
        {"rule_id": "GO-STYLE-001", "file_path": "other.go",
         "line_start": 1, "title": "x", "body": "x", "evidence": "x"},
    ]})
    parsed = parse_rule_response(unknown, go_batch)
    assert parsed.findings == ()
    assert len(parsed.rejections) == 2
```

- [ ] **Step 4: Implement parser and serialization compatibility**

Accept one JSON object with a `findings` list. Validate bounded strings, batch rule IDs, exact batch file paths and positive line numbers. Never evaluate Markdown fences. Build only `Finding(confidence="advisory", severity=rule.severity, rule_id=rule.id)`. Ensure old serialized Finding/Trace dictionaries remain loadable through defaults.

- [ ] **Step 5: Run and commit**

Run: `pytest -q tests/test_models.py tests/test_rule_review.py`

```bash
git add review_agent/models.py review_agent/rule_review.py tests/test_rule_review.py tests/test_models.py
git commit -m "feat: build and validate MDR review batches"
```

### Task 4: Budgeted MDR Pipeline Stage, Traces, and Checkpoint Invalidation

**Files:**
- Modify: `review_agent/pipeline.py`
- Modify: `review_agent/report.py`
- Modify: `review_agent/storage.py`
- Create: `tests/test_mdr_pipeline.py`

**Interfaces:**
- Extend `ReviewPipeline(..., rules: RuleRegistry | None = None)` without breaking existing callers.
- Checkpoint key: `rules:<language>:<batch_index>`.
- Reservation checkpoint key: `rules:<language>:<batch_index>:reservation`.
- Checkpoint payload: `ruleset_hash`, `diff_hash`, `findings`, `batch_trace_id`, `finding_trace_ids`.
- Trace kinds: `mdr_batch` and `mdr_finding`.

Create these concrete helpers at the top of `tests/test_mdr_pipeline.py`:

```python
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
```

- [ ] **Step 1: Write failing one-call-per-language test**

```python
def test_pipeline_calls_llm_once_for_multiple_go_rules(tmp_path):
    client = JsonRuleClient({"findings": []})
    result = pipeline_with_rules(
        tmp_path, diff=GO_DIFF,
        rules=(make_rule(), make_rule("GO-ERROR-001")), client=client,
    ).run("local://go")
    assert client.rule_calls == 1
    assert any(trace["kind"] == "mdr_batch" for trace in result.traces)
```

- [ ] **Step 2: Verify RED**

Run: `pytest -q tests/test_mdr_pipeline.py::test_pipeline_calls_llm_once_for_multiple_go_rules`

Expected: FAIL because pipeline does not accept a rule registry.

- [ ] **Step 3: Add the rule-review stage**

After sanitize and deterministic tools, split sanitized diff by language. For each language with rules, build batches and use existing `BudgetController`, `StateStore.reserve_budget`, `LLMClient.review`, and `StateStore.settle_reservation`. Before each request, persist its token, reserved amount, hashes and `in_flight` status under the reservation checkpoint key. On recovery, conservatively skip an unresolved matching reservation and emit a recovery trace instead of issuing a potentially duplicate paid request. Execute MDR batches before generic advisory review so organization rules receive budget priority. No per-rule branch is added to pipeline.

Create an `mdr_batch` trace containing redacted prompt/response, rule IDs, hashes, usage and rejection reasons in trace metadata. Create one `mdr_finding` trace per accepted finding with `rule_id` and `parent_trace_id`; set its trace ID on the Finding.

The Markdown trace appendix must display `mdr_batch`, its rule ID list and `ruleset_hash`, even when a valid batch produces zero findings. MDR findings display rule ID and severity next to the advisory confidence label.

- [ ] **Step 4: Write checkpoint invalidation tests**

```python
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
```

- [ ] **Step 5: Implement hash-aware reuse and render invalidation**

Reuse a batch checkpoint only if its `ruleset_hash` and `diff_hash` match. Ignore obsolete batch checkpoints. Compute the render input hash from active finding IDs and active batch trace IDs; regenerate Markdown when it changes so rule updates cannot return stale reports.

- [ ] **Step 6: Assert confidence and trace relationships**

```python
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
```

- [ ] **Step 7: Run regression tests and commit**

Run: `pytest -q tests/test_mdr_pipeline.py tests/test_pipeline_report.py tests/test_storage.py`

Expected: PASS with generic review, budget caps, recovery and report redaction unchanged.

```bash
git add review_agent/pipeline.py review_agent/report.py review_agent/storage.py tests/test_mdr_pipeline.py
git commit -m "feat: execute MDR rules in resumable language batches"
```

### Task 5: CLI Rule Discovery and Failure UX

**Files:**
- Modify: `review_agent/cli.py`
- Modify: `tests/test_cli_e2e.py`
- Create: `tests/test_cli_rules.py`

**Interfaces:**
- Repeatable `--rules-dir PATH`.
- Optional `--config PATH`.
- Default `~/.config/code-review-agent/rules.d` only when it exists.
- CLI constructs and passes one `RuleRegistry` to pipeline.

- [ ] **Step 1: Write failing CLI discovery test**

```python
def test_cli_loads_toml_and_repeated_rule_directories(tmp_path, monkeypatch):
    rc = main([
        "review", "--diff-file", str(GO_DIFF_FILE), "--offline",
        "--config", str(CONFIG_FILE),
        "--rules-dir", str(EXTRA_RULES_A),
        "--rules-dir", str(EXTRA_RULES_B),
        "--state-dir", str(tmp_path / "state"),
        "--output", str(tmp_path / "report.md"),
    ])
    assert rc == 0
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "COMMON-SECURITY-001" in report
    assert "GO-STYLE-001" in report
```

- [ ] **Step 2: Verify RED and wire flags**

Run: `pytest -q tests/test_cli_rules.py::test_cli_loads_toml_and_repeated_rule_directories`

Resolve TOML paths against its parent and CLI paths against current working directory. Add existing default user directory first. Load MDR files, register/filter them, and pass `rules=registry` to pipeline. No-rule invocation must retain current behavior.

- [ ] **Step 3: Add explicit configuration error test**

```python
def test_cli_reports_rule_file_and_reason_for_invalid_mdr(tmp_path, capsys):
    bad = tmp_path / "bad.mdr"
    bad.write_text("---\nid: invalid id\n---\n", encoding="utf-8")
    rc = main(["review", "--diff-file", str(GO_DIFF_FILE), "--offline",
               "--rules-dir", str(tmp_path)])
    assert rc == 1
    error = capsys.readouterr().err
    assert "bad.mdr" in error
    assert "invalid rule id" in error
```

- [ ] **Step 4: Run and commit**

Run: `pytest -q tests/test_cli_rules.py tests/test_cli_e2e.py`

Expected: PASS, including `--run-id` recovery and no-rules compatibility.

```bash
git add review_agent/cli.py tests/test_cli_rules.py tests/test_cli_e2e.py
git commit -m "feat: configure MDR rules from CLI and TOML"
```

### Task 6: Example MDR, Documentation, and Offline E2E

**Files:**
- Create: `examples/rules/go/GO-STYLE-001.mdr`
- Create: `review-agent.example.toml`
- Modify: `README.md`
- Modify: `review_agent/llm.py`
- Create: `tests/fixtures/go-many-parameters.diff`
- Modify: `tests/test_cli_rules.py`

**Interfaces:**
- Documented offline command loads the example config and rules directory.

- [ ] **Step 1: Add approved example artifacts**

Create `GO-STYLE-001.mdr` with the approved YAML front matter, Chinese explanation and fenced Go positive/negative examples. Create `review-agent.example.toml` with `enabled_languages = ["go"]`, `directories = []`, and `disabled_rules = []`; the command supplies `examples/rules` explicitly.

- [ ] **Step 2: Write failing offline E2E test**

```python
def test_offline_cli_loads_example_rule_and_emits_batch_trace(tmp_path):
    output = tmp_path / "review.md"
    rc = main([
        "review", "--diff-file", str(GO_MANY_PARAMETERS_DIFF),
        "--config", str(EXAMPLE_CONFIG),
        "--rules-dir", str(EXAMPLE_RULES),
        "--output", str(output), "--state-dir", str(tmp_path / "state"),
        "--offline",
    ])
    report = output.read_text(encoding="utf-8")
    assert rc == 0
    assert "GO-STYLE-001" in report
    assert "mdr_batch" in report
    assert "trace-" in report
```

- [ ] **Step 3: Make offline MDR responses valid JSON**

When `DeterministicClient` detects the MDR JSON response contract, return exactly `{"findings": []}`. Do not hard-code a GO-STYLE violation; structured finding behavior is covered by injected JSON clients.

- [ ] **Step 4: Update README**

Document MDR fields, a minimal example, directory precedence, language mappings, Common rules, advisory-only confidence, schema errors, deprecated/disabled behavior, checkpoint hashes, and the guarantee that rule code blocks are never executed.

- [ ] **Step 5: Run full verification**

Run: `pytest -q`

Run: `python3 -m review_agent --help`

Run: `python3 -m review_agent review --diff-file tests/fixtures/go-many-parameters.diff --config review-agent.example.toml --rules-dir examples/rules --output /tmp/mdr-review.md --state-dir /tmp/mdr-review-state --offline`

Run: `git diff --check`

Expected: all tests PASS; commands exit 0; report contains `GO-STYLE-001`, `mdr_batch` and trace IDs, with no secret or absolute MDR path.

- [ ] **Step 6: Commit**

```bash
git add examples/rules/go/GO-STYLE-001.mdr review-agent.example.toml README.md \
  review_agent/llm.py tests/fixtures/go-many-parameters.diff tests/test_cli_rules.py
git commit -m "docs: add MDR rule authoring example"
```

## Final Verification

- [ ] Run `pytest -q` and confirm zero failures.
- [ ] Run the documented offline MDR command and inspect its Markdown.
- [ ] Confirm multiple rules for one language produce one batch request.
- [ ] Confirm all pure MDR findings remain advisory.
- [ ] Confirm rule/diff secrets never appear in persisted prompts or reports.
- [ ] Confirm changing only one language ruleset reruns only that language checkpoint.
- [ ] Run `git diff --check` and confirm `git status --short` contains only intended files.
