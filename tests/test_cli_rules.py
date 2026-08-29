from pathlib import Path

from review_agent.cli import main
from review_agent.models import ChangeRequest, RunConfig
from review_agent.storage import StateStore


EXAMPLE_CONFIG = Path(__file__).parents[1] / "review-agent.example.toml"
EXAMPLE_RULES = Path(__file__).parents[1] / "examples" / "rules"
GO_MANY_PARAMETERS_DIFF = Path(__file__).parent / "fixtures" / "go-many-parameters.diff"


GO_DIFF = """diff --git a/internal/user.go b/internal/user.go
--- a/internal/user.go
+++ b/internal/user.go
@@ -1,1 +1,2 @@
+func CreateUser(name string, age int, role string, active bool, region string) {}
"""

RULE = """---
id: {rule_id}
title: {title}
language: {language}
domains: [STYLE]
severity: warning
prompt_hint: Check changed code.
deprecated: false
---
# {rule_id}
"""


def _write_go_diff(path: Path) -> None:
    path.write_text(GO_DIFF, encoding="utf-8")


def _write_rule(directory: Path, rule_id: str, language: str = "go") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{rule_id}.mdr").write_text(
        RULE.format(rule_id=rule_id, title=rule_id, language=language),
        encoding="utf-8",
    )


def test_cli_loads_toml_and_repeated_rule_directories(tmp_path):
    diff_file = tmp_path / "change.diff"
    _write_go_diff(diff_file)
    config_rules = tmp_path / "config-rules"
    extra_a = tmp_path / "extra-a"
    extra_b = tmp_path / "extra-b"
    _write_rule(config_rules, "GO-STYLE-001")
    _write_rule(extra_a, "COMMON-SECURITY-001", "common")
    _write_rule(extra_b, "GO-STYLE-002")
    config_file = tmp_path / "review-agent.toml"
    config_file.write_text(
        '[rules]\ndirectories = ["config-rules"]\n', encoding="utf-8"
    )
    output = tmp_path / "report.md"
    rc = main([
        "review", "--diff-file", str(diff_file), "--offline",
        "--config", str(config_file), "--rules-dir", str(extra_a),
        "--rules-dir", str(extra_b), "--state-dir", str(tmp_path / "state"),
        "--output", str(output),
    ])
    assert rc == 0
    report = output.read_text(encoding="utf-8")
    assert "GO-STYLE-001" in report
    assert "COMMON-SECURITY-001" in report
    assert "GO-STYLE-002" in report


def test_cli_loads_existing_default_user_rule_directory(tmp_path, monkeypatch):
    diff_file = tmp_path / "change.diff"
    _write_go_diff(diff_file)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    default_rules = tmp_path / "home" / ".config" / "code-review-agent" / "rules.d"
    _write_rule(default_rules, "GO-DEFAULT-001")
    output = tmp_path / "report.md"
    assert main([
        "review", "--diff-file", str(diff_file), "--offline",
        "--state-dir", str(tmp_path / "state"), "--output", str(output),
    ]) == 0
    assert "GO-DEFAULT-001" in output.read_text(encoding="utf-8")


def test_cli_reports_rule_file_and_reason_for_invalid_mdr(tmp_path, capsys):
    diff_file = tmp_path / "change.diff"
    _write_go_diff(diff_file)
    bad = tmp_path / "bad.mdr"
    bad.write_text("---\nid: invalid id\n---\n", encoding="utf-8")
    rc = main([
        "review", "--diff-file", str(diff_file), "--offline",
        "--rules-dir", str(tmp_path), "--state-dir", str(tmp_path / "state"),
        "--output", str(tmp_path / "report.md"),
    ])
    assert rc == 1
    error = capsys.readouterr().err
    assert "bad.mdr" in error
    assert "invalid rule id" in error


def test_cli_duplicate_rules_report_both_directories(tmp_path, capsys):
    diff_file = tmp_path / "change.diff"
    _write_go_diff(diff_file)
    first, second = tmp_path / "first", tmp_path / "second"
    _write_rule(first, "GO-DUP-001")
    _write_rule(second, "GO-DUP-001")
    assert main(["review", "--diff-file", str(diff_file), "--offline", "--rules-dir", str(first),
                 "--rules-dir", str(second), "--state-dir", str(tmp_path / "state"),
                 "--output", str(tmp_path / "report.md")]) == 1
    error = capsys.readouterr().err
    assert str(first / "GO-DUP-001.mdr") in error
    assert str(second / "GO-DUP-001.mdr") in error


def test_cli_run_id_uses_persisted_remote_url_and_reuses_fetch(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    store = StateStore(state_dir / "state.db")
    run_id = store.create_run(RunConfig(
        url="https://github.com/acme/project/pull/7", budget_usd=1.0,
        offline=True, state_dir=str(state_dir),
    ))
    request = ChangeRequest(
        url="https://github.com/acme/project/pull/7", title="cached",
        diff=GO_DIFF, source="github",
    )
    store.save_checkpoint(run_id, "fetch", {"request": request.to_dict(), "diff_hash": "cached"})
    selected = {}

    class StubGitHubAdapter:
        def __init__(self, token=None):
            selected["adapter"] = "github"

        def fetch(self, url):
            raise AssertionError("cached fetch checkpoint should be reused")

    monkeypatch.setattr("review_agent.cli.GitHubAdapter", StubGitHubAdapter)
    output = tmp_path / "report.md"
    assert main(["review", "--run-id", run_id, "--offline",
                 "--state-dir", str(state_dir), "--output", str(output)]) == 0
    assert selected["adapter"] == "github"


def test_cli_run_id_local_without_fetch_requires_original_diff(tmp_path, capsys):
    state_dir = tmp_path / "state"
    store = StateStore(state_dir / "state.db")
    run_id = store.create_run(RunConfig(
        url="local:///tmp/original.diff", budget_usd=1.0,
        offline=True, state_dir=str(state_dir),
    ))
    assert main(["review", "--run-id", run_id, "--offline",
                 "--state-dir", str(state_dir), "--output", str(tmp_path / "report.md")]) == 1
    assert "原始 diff" in capsys.readouterr().err


def test_offline_cli_loads_example_rule_and_emits_batch_trace(tmp_path):
    output = tmp_path / "review.md"
    rc = main([
        "review", "--diff-file", str(GO_MANY_PARAMETERS_DIFF),
        "--config", str(EXAMPLE_CONFIG), "--rules-dir", str(EXAMPLE_RULES),
        "--output", str(output), "--state-dir", str(tmp_path / "state"),
        "--offline",
    ])
    report = output.read_text(encoding="utf-8")
    assert rc == 0
    assert "GO-STYLE-001" in report
    assert "mdr_batch" in report
    assert "trace-" in report


def test_run_id_resume_uses_persisted_mdr_snapshot_without_rule_flags(tmp_path):
    diff_file = tmp_path / "change.diff"
    _write_go_diff(diff_file)
    rules_dir = tmp_path / "rules"
    _write_rule(rules_dir, "GO-SNAPSHOT-001")
    state_dir = tmp_path / "state"
    output = tmp_path / "first.md"
    assert main(["review", "--diff-file", str(diff_file), "--rules-dir", str(rules_dir),
                 "--offline", "--state-dir", str(state_dir), "--output", str(output)]) == 0
    store = StateStore(state_dir / "state.db")
    run = store.find_latest_run(f"local://{diff_file.resolve()}#sha256=" + __import__("hashlib").sha256(diff_file.read_bytes()).hexdigest())
    assert run is not None
    resumed = tmp_path / "resumed.md"
    assert main(["review", "--run-id", run["run_id"], "--offline", "--state-dir", str(state_dir), "--output", str(resumed)]) == 0
    assert "GO-SNAPSHOT-001" in resumed.read_text(encoding="utf-8")


def test_cli_accepts_explicit_self_hosted_gitlab_allowlist(tmp_path, monkeypatch):
    monkeypatch.setattr("review_agent.cli.GitLabAdapter", lambda token=None: type("A", (), {"fetch": lambda self, url: ChangeRequest(url=url, diff=GO_DIFF, source="gitlab")})())
    output = tmp_path / "report.md"
    assert main(["review", "https://git.example.com/group/project/-/merge_requests/3",
                 "--gitlab-host", "git.example.com", "--offline", "--state-dir", str(tmp_path / "state"),
                 "--output", str(output)]) == 0


def test_cli_passes_provider_token_from_toml_to_adapter(tmp_path, monkeypatch):
    seen = {}

    class StubGitHubAdapter:
        def __init__(self, token=None):
            seen["token"] = token

        def fetch(self, url):
            return ChangeRequest(url=url, diff=GO_DIFF, source="github")

    monkeypatch.setattr("review_agent.cli.GitHubAdapter", StubGitHubAdapter)
    config_file = tmp_path / "review-agent.toml"
    config_file.write_text('[review]\noffline = true\n[github]\ntoken = "github-from-file"\n', encoding="utf-8")
    assert main([
        "review", "https://github.com/acme/project/pull/7",
        "--config", str(config_file), "--state-dir", str(tmp_path / "state"),
        "--output", str(tmp_path / "report.md"),
    ]) == 0
    assert seen["token"] == "github-from-file"


def test_cli_applies_profile_rules_and_skip_globs(tmp_path):
    diff_file = tmp_path / "change.diff"
    _write_go_diff(diff_file)
    rules_dir = tmp_path / "rules"
    _write_rule(rules_dir, "GO-PROFILE-001")
    profile_file = tmp_path / "profiles.toml"
    profile_file.write_text(
        """[[profiles]]
name = "sample"
repo = "/workspace/sample"
rules_dirs = ["rules"]
enabled_languages = ["go"]
skip_globs = ["vendor/**"]
""".replace("/workspace/sample", str(tmp_path / "repo")),
        encoding="utf-8",
    )
    output = tmp_path / "report.md"
    assert main([
        "review", "--diff-file", str(diff_file), "--offline",
        "--profile", str(profile_file), "--repo-path", str(tmp_path / "repo"),
        "--output", str(output), "--state-dir", str(tmp_path / "state"),
    ]) == 0
    assert "GO-PROFILE-001" in output.read_text(encoding="utf-8")


def test_profile_language_allowlist_filters_other_rules(tmp_path):
    diff_file = tmp_path / "change.diff"
    _write_go_diff(diff_file)
    rules_dir = tmp_path / "rules"
    _write_rule(rules_dir, "GO-PROFILE-001")
    profile_file = tmp_path / "profiles.toml"
    profile_file.write_text(
        """[[profiles]]
name = "python-only"
repo = "REPO_PATH"
rules_dirs = ["rules"]
enabled_languages = ["python"]
""".replace("REPO_PATH", str(tmp_path / "repo")), encoding="utf-8")
    output = tmp_path / "report.md"
    assert main([
        "review", "--diff-file", str(diff_file), "--offline",
        "--profile", str(profile_file), "--repo-path", str(tmp_path / "repo"),
        "--output", str(output), "--state-dir", str(tmp_path / "state"),
    ]) == 0
    assert "GO-PROFILE-001" not in output.read_text(encoding="utf-8")


def test_cli_uses_unified_review_and_llm_config(tmp_path):
    diff_file = tmp_path / "change.diff"
    _write_go_diff(diff_file)
    config_file = tmp_path / "review-agent.toml"
    config_file.write_text(
        """[review]
budget_usd = 3.5
max_diff_chars = 4096
completion_tokens = 128
offline = true
output = "configured-report.md"
state_dir = "configured-state"

[llm]
model = "custom-primary"
fallback_model = "custom-fallback"
timeout_seconds = 9

[llm.pricing]
custom-primary = 0.02
custom-fallback = 0.01
""",
        encoding="utf-8",
    )
    assert main(["review", "--diff-file", str(diff_file), "--config", str(config_file)]) == 0
    state_dir = tmp_path / "configured-state"
    store = StateStore(state_dir / "state.db")
    run = store.find_latest_run(
        f"local://{diff_file.resolve()}#sha256="
        + __import__("hashlib").sha256(diff_file.read_bytes()).hexdigest()
    )
    assert run is not None
    assert run["config"]["model"] == "custom-primary"
    assert run["config"]["budget_usd"] == 3.5
    assert run["config"]["model_pricing"]["custom-primary"] == 0.02
    assert (tmp_path / "configured-report.md").is_file()
