from pathlib import Path

from review_agent.config import load_app_config
from review_agent.cli import DEFAULT_CONFIG_PATH
from review_agent.cli import main


def test_default_config_file_is_project_conf():
    assert DEFAULT_CONFIG_PATH == Path(__file__).parents[1] / "conf" / "review-agent.toml"
    assert DEFAULT_CONFIG_PATH.is_file()


def test_cli_can_use_default_config_without_config_argument(tmp_path):
    config = load_app_config(DEFAULT_CONFIG_PATH, environ={})
    assert config.review_mode == "mdr_only"
    assert config.model
    assert config.budget_usd > 0


def test_cli_review_works_with_only_diff_and_offline_flags(tmp_path):
    diff = tmp_path / "change.diff"
    diff.write_text(
        "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n+pass\n",
        encoding="utf-8",
    )
    output = tmp_path / "report.md"
    assert main([
        "review", "--diff-file", str(diff), "--offline",
        "--output", str(output), "--state-dir", str(tmp_path / "state"),
    ]) == 0
    assert output.is_file()
