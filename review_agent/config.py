"""TOML configuration for explicitly authorized rule directories."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RulesConfig:
    directories: tuple[Path, ...] = ()
    enabled_languages: tuple[str, ...] = ()
    disabled_rules: tuple[str, ...] = ()


def load_rules_config(path: str | Path | None = None,
                      cli_directories: list[str | Path] | None = None) -> RulesConfig:
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
