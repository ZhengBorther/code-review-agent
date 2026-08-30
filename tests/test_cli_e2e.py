from pathlib import Path

from review_agent.cli import main
from review_agent.storage import StateStore


FIXTURE = Path(__file__).parent / "fixtures" / "sample.diff"


def test_cli_generates_report_without_network(tmp_path):
    output = tmp_path / "report.md"
    assert main(["review", "--diff-file", str(FIXTURE), "--output", str(output), "--offline", "--state-dir", str(tmp_path / "state")]) == 0
    content = output.read_text(encoding="utf-8")
    assert "Code Review" in content
    assert "可直接采纳" in content
    assert "trace-" in content


def test_cli_help_exits_zero(capsys):
    assert main(["--help"]) == 0
    output = capsys.readouterr().out
    assert "review" in output


def test_review_help_exposes_log_level(capsys):
    assert main(["review", "--help"]) == 0
    assert "--log-level" in capsys.readouterr().out


def test_cli_reuses_existing_run_for_same_url(tmp_path):
    state_dir = tmp_path / "state"
    output = tmp_path / "report.md"
    args = ["review", "--diff-file", str(FIXTURE), "--output", str(output), "--offline", "--state-dir", str(state_dir)]
    assert main(args) == 0
    assert main(args) == 0
    store = StateStore(state_dir / "state.db")
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_cli_accepts_explicit_run_id(tmp_path):
    state_dir = tmp_path / "state"
    output = tmp_path / "report.md"
    base = ["review", "--diff-file", str(FIXTURE), "--output", str(output), "--offline", "--state-dir", str(state_dir)]
    assert main(base) == 0
    with StateStore(state_dir / "state.db")._connect() as conn:
        run_id = conn.execute("SELECT run_id FROM runs").fetchone()[0]
    assert main(["review", "--run-id", run_id, "--output", str(output), "--offline", "--state-dir", str(state_dir)]) == 0


def test_cli_local_diff_identity_changes_with_input(tmp_path):
    state_dir = tmp_path / "state"
    first = tmp_path / "one.diff"
    second = tmp_path / "two.diff"
    first.write_text("diff --git a/a.py b/a.py\n+ # TODO one\n", encoding="utf-8")
    second.write_text("diff --git a/b.py b/b.py\n+ # TODO two\n", encoding="utf-8")
    for diff in (first, second):
        assert main(["review", "--diff-file", str(diff), "--output", str(tmp_path / (diff.stem + ".md")), "--offline", "--state-dir", str(state_dir)]) == 0
    with StateStore(state_dir / "state.db")._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
