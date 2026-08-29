"""TOML configuration for explicitly authorized rule directories."""

from __future__ import annotations

import tomllib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class RulesConfig:
    directories: tuple[Path, ...] = ()
    enabled_languages: tuple[str, ...] = ()
    disabled_rules: tuple[str, ...] = ()


@dataclass(frozen=True)
class AppConfig:
    """Resolved runtime configuration; secrets are supplied by the environment."""

    budget_usd: float = 1.0
    model: str = "large"
    fallback_model: str = "small"
    max_diff_chars: int = 12000
    completion_tokens: int = 512
    output_path: str = "review.md"
    state_dir: str = ".review-state"
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_timeout_seconds: float = 30.0
    model_pricing: dict[str, float] = field(default_factory=dict)
    offline: bool = False
    rules: RulesConfig = field(default_factory=RulesConfig)
    gitlab_allowed_hosts: tuple[str, ...] = ()


def _table(data: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"[{name}] must be a table")
    return value


def load_app_config(
    path: str | Path | None = None,
    cli_directories: list[str | Path] | None = None,
    overrides: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> AppConfig:
    """Resolve defaults, TOML, environment, then explicit CLI overrides."""
    env = dict(os.environ if environ is None else environ)
    data: dict[str, Any] = {}
    config_path: Path | None = None
    if path is not None:
        config_path = Path(path).resolve()
        with config_path.open("rb") as stream:
            data = tomllib.load(stream)
    review = _table(data, "review")
    llm = _table(data, "llm")
    pricing_data = llm.get("pricing", {})
    if not isinstance(pricing_data, dict):
        raise ValueError("[llm.pricing] must be a table")
    if "api_key" in llm:
        raise ValueError("llm.api_key must not be stored in TOML; use ONEAPI_API_KEY")

    def value(file_section: dict[str, Any], file_key: str, env_key: str, default: Any) -> Any:
        if env_key in env:
            return env[env_key]
        return file_section.get(file_key, default)

    def boolean(raw: Any, name: str) -> bool:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.lower() in ("true", "false", "1", "0"):
            return raw.lower() in ("true", "1")
        raise ValueError(f"{name} must be a boolean")

    resolved: dict[str, Any] = {
        "budget_usd": float(value(review, "budget_usd", "REVIEW_BUDGET_USD", 1.0)),
        "model": str(value(llm, "model", "ONEAPI_MODEL", "large")),
        "fallback_model": str(value(llm, "fallback_model", "ONEAPI_FALLBACK_MODEL", "small")),
        "max_diff_chars": int(value(review, "max_diff_chars", "REVIEW_MAX_DIFF_CHARS", 12000)),
        "completion_tokens": int(value(review, "completion_tokens", "REVIEW_COMPLETION_TOKENS", 512)),
        "output_path": str(value(review, "output", "REVIEW_OUTPUT", "review.md")),
        "state_dir": str(value(review, "state_dir", "REVIEW_STATE_DIR", ".review-state")),
        "llm_base_url": value(llm, "base_url", "ONEAPI_BASE_URL", None),
        "llm_api_key": env.get("ONEAPI_API_KEY") or env.get("OPENAI_API_KEY"),
        "llm_timeout_seconds": float(value(llm, "timeout_seconds", "ONEAPI_TIMEOUT_SECONDS", 30.0)),
        "offline": boolean(value(review, "offline", "REVIEW_OFFLINE", False), "review.offline"),
    }
    # File-owned paths follow the config file, while environment and CLI paths
    # retain normal process-working-directory semantics.
    if config_path is not None and "REVIEW_OUTPUT" not in env and "output" in review:
        output = Path(resolved["output_path"])
        resolved["output_path"] = str(output if output.is_absolute() else config_path.parent / output)
    if config_path is not None and "REVIEW_STATE_DIR" not in env and "state_dir" in review:
        state = Path(resolved["state_dir"])
        resolved["state_dir"] = str(state if state.is_absolute() else config_path.parent / state)
    if pricing_data:
        resolved["model_pricing"] = {str(model): float(rate) for model, rate in pricing_data.items()}
    else:
        resolved["model_pricing"] = {}
    security = _table(data, "security")
    hosts = security.get("gitlab_allowed_hosts", [])
    if not isinstance(hosts, list) or not all(isinstance(item, str) for item in hosts):
        raise ValueError("security.gitlab_allowed_hosts must contain strings")
    resolved["gitlab_allowed_hosts"] = tuple(hosts)
    for key, item in (overrides or {}).items():
        if item is not None:
            resolved[key] = item
    if any(rate <= 0 for rate in resolved["model_pricing"].values()):
        raise ValueError("llm pricing rates must be positive")
    if resolved["budget_usd"] < 0 or resolved["max_diff_chars"] <= 0 or resolved["completion_tokens"] <= 0:
        raise ValueError("review budget and token limits must be positive")
    if resolved["llm_timeout_seconds"] <= 0:
        raise ValueError("llm timeout_seconds must be positive")
    return AppConfig(rules=load_rules_config(path, cli_directories), **resolved)


def load_rules_config(path: str | Path | None = None,
                      cli_directories: list[str | Path] | None = None) -> RulesConfig:
    # TOML paths are relative to the config file; CLI paths are relative to
    # the process working directory, matching normal command-line semantics.
    directories: list[Path] = []
    enabled: tuple[str, ...] = ()
    disabled: tuple[str, ...] = ()
    if path is not None:
        config_path = Path(path).resolve()
        with config_path.open("rb") as stream:
            data = tomllib.load(stream)
        section = data.get("rules", {})
        if not isinstance(section, dict):
            raise ValueError("[rules] must be a table")
        raw_directories = section.get("directories", [])
        if not isinstance(raw_directories, list):
            raise ValueError("rules.directories must be an array")
        for item in raw_directories:
            if not isinstance(item, str):
                raise ValueError("rules.directories must contain strings")
            directories.append((config_path.parent / item).resolve())
        raw_enabled = section.get("enabled_languages", [])
        raw_disabled = section.get("disabled_rules", [])
        if (not isinstance(raw_enabled, list) or
                not all(isinstance(item, str) for item in raw_enabled)):
            raise ValueError("rules.enabled_languages must contain strings")
        if (not isinstance(raw_disabled, list) or
                not all(isinstance(item, str) for item in raw_disabled)):
            raise ValueError("rules.disabled_rules must contain strings")
        enabled = tuple(item.lower() for item in raw_enabled)
        disabled = tuple(raw_disabled)
    for item in cli_directories or []:
        directories.append(Path(item).resolve())
    # Preserve declaration order while preventing duplicate directory scans.
    directories = list(dict.fromkeys(directories))
    return RulesConfig(tuple(directories), enabled, disabled)
