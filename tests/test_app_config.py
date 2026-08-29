import pytest

from review_agent.config import load_app_config
from review_agent.models import RunConfig


def test_load_app_config_reads_review_llm_and_pricing_sections(tmp_path):
    config_file = tmp_path / "review-agent.toml"
    config_file.write_text(
        """[review]
budget_usd = 10.0
max_diff_chars = 8000
completion_tokens = 256
output = "out.md"
state_dir = ".state"

[llm]
base_url = "https://oneapi.example/v1"
model = "qwen-plus"
fallback_model = "qwen-turbo"
timeout_seconds = 12

[llm.pricing]
qwen-plus = 0.003
qwen-turbo = 0.001
""",
        encoding="utf-8",
    )
    config = load_app_config(config_file)
    assert config.budget_usd == 10.0
    assert config.model == "qwen-plus"
    assert config.fallback_model == "qwen-turbo"
    assert config.model_pricing == {"qwen-plus": 0.003, "qwen-turbo": 0.001}
    assert config.output_path == str(tmp_path / "out.md")
    assert config.llm_timeout_seconds == 12


def test_cli_overrides_file_and_environment(monkeypatch, tmp_path):
    config_file = tmp_path / "review-agent.toml"
    config_file.write_text('[llm]\nmodel = "from-file"\n', encoding="utf-8")
    monkeypatch.setenv("ONEAPI_MODEL", "from-env")
    config = load_app_config(config_file, overrides={"model": "from-cli", "budget_usd": 2.5})
    assert config.model == "from-cli"
    assert config.budget_usd == 2.5


def test_environment_overrides_file(monkeypatch, tmp_path):
    config_file = tmp_path / "review-agent.toml"
    config_file.write_text('[llm]\nmodel = "from-file"\n', encoding="utf-8")
    monkeypatch.setenv("ONEAPI_MODEL", "from-env")
    assert load_app_config(config_file).model == "from-env"


def test_credentials_can_be_loaded_from_toml_without_being_run_config(tmp_path):
    config_file = tmp_path / "review-agent.toml"
    config_file.write_text(
        """[llm]
api_key = "oneapi-secret"

[github]
token = "github-secret"

[gitlab]
token = "gitlab-secret"
""",
        encoding="utf-8",
    )
    config = load_app_config(config_file, environ={})
    assert config.llm_api_key == "oneapi-secret"
    assert config.github_token == "github-secret"
    assert config.gitlab_token == "gitlab-secret"
    assert "llm_api_key" not in RunConfig(url="x").to_dict()
