from pathlib import Path

from review_agent.cli import main


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
    assert "review" in capsys.readouterr().out
