"""Safe, data-only loading of Markdown review rules."""

from __future__ import annotations

import re
import collections
import hashlib
import json
from collections import abc
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# Older PyYAML releases still refer to the pre-3.10 collections aliases.
if not hasattr(collections, "Hashable"):
    collections.Hashable = abc.Hashable


class RuleLoadError(ValueError):
    """Raised when an MDR file is invalid or unsafe to load."""


@dataclass(frozen=True)
class ReviewRule:
    id: str
    title: str
    language: str
    domains: tuple[str, ...]
    severity: str
    prompt_hint: str
    deprecated: bool
    body: str
    source: str


class RuleRegistry:
    """Registry for data-only MDR rules and their effective configuration."""

    def __init__(self, config: Any):
        self.config = config
        self._rules: dict[str, ReviewRule] = {}

    def register(self, rule: ReviewRule) -> None:
        if rule.id in self._rules:
            existing = self._rules[rule.id]
            raise RuleLoadError(
                f"duplicate rule id: {rule.id} (sources: {existing.source}, {rule.source})"
            )
        self._rules[rule.id] = rule

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {"id": rule.id, "title": rule.title, "language": rule.language,
             "domains": list(rule.domains), "severity": rule.severity,
             "prompt_hint": rule.prompt_hint, "deprecated": rule.deprecated,
             "body": rule.body, "source": rule.source}
            for rule in sorted(self._rules.values(), key=lambda item: item.id)
        ]

    @classmethod
    def from_snapshot(cls, config: Any, snapshot: list[dict[str, Any]]) -> "RuleRegistry":
        registry = cls(config)
        for item in snapshot:
            registry.register(ReviewRule(
                id=item["id"], title=item["title"], language=item["language"],
                domains=tuple(item.get("domains", ())), severity=item["severity"],
                prompt_hint=item["prompt_hint"], deprecated=bool(item.get("deprecated", False)),
                body=item.get("body", ""), source=item.get("source", "snapshot")))
        return registry

    def applicable(self, language: str) -> tuple[ReviewRule, ...]:
        target = language.lower()
        enabled = {item.lower() for item in getattr(self.config, "enabled_languages", ())}
        if enabled and target != "unknown" and target not in enabled:
            return ()
        disabled = set(getattr(self.config, "disabled_rules", ()))
        rules = (rule for rule in self._rules.values()
                 if rule.language in (target, "common")
                 and rule.id not in disabled and not rule.deprecated)
        return tuple(sorted(rules, key=lambda rule: rule.id))

    def ruleset_hash(self, language: str) -> str:
        effective = [
            {"id": rule.id, "title": rule.title, "language": rule.language,
             "domains": list(rule.domains), "severity": rule.severity,
             "prompt_hint": rule.prompt_hint, "deprecated": rule.deprecated,
             "body": rule.body, "source": rule.source}
            for rule in self.applicable(language)
        ]
        payload = {"language": language.lower(), "rules": effective,
                   "enabled_languages": sorted(
                       item.lower() for item in getattr(self.config, "enabled_languages", ())),
                   "disabled_rules": sorted(getattr(self.config, "disabled_rules", ()))}
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


_ID_RE = re.compile(r"^[A-Z][A-Z0-9_-]+$")
_SEVERITIES = {"error", "warning", "info"}
_REQUIRED = ("id", "title", "language", "domains", "severity", "prompt_hint", "deprecated")


class MdrRuleLoader:
    def __init__(self, max_file_bytes: int = 262_144):
        self.max_file_bytes = max_file_bytes

    def load(self, directory: str | Path) -> list[ReviewRule]:
        root = Path(directory).resolve()
        if not root.is_dir():
            raise RuleLoadError(f"rule directory does not exist: {directory}")
        result = []
        seen: dict[str, str] = {}
        for path in sorted(root.rglob("*.mdr")):
            if path.is_symlink():
                raise RuleLoadError(f"symlink rule file is not allowed: {path.name}")
            try:
                path.resolve().relative_to(root)
            except ValueError as exc:
                raise RuleLoadError(f"rule file escapes authorized directory: {path.name}") from exc
            try:
                size = path.stat().st_size
                if size > self.max_file_bytes:
                    raise RuleLoadError(f"rule file too large: {path.name}")
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise RuleLoadError(f"invalid UTF-8 in {path.name}") from exc
            rule = self._parse(text, path.relative_to(root).as_posix())
            if rule.id in seen:
                raise RuleLoadError(f"duplicate rule id: {rule.id} (sources: {seen[rule.id]}, {path.relative_to(root).as_posix()})")
            seen[rule.id] = path.relative_to(root).as_posix()
            if not rule.deprecated:
                result.append(rule)
        return result

    def _parse(self, text: str, source: str) -> ReviewRule:
        lines = text.splitlines(keepends=True)
        if not lines or lines[0].rstrip("\r\n") != "---":
            raise RuleLoadError(f"invalid YAML front matter in {source}")
        end = next((index for index, line in enumerate(lines[1:], 1)
                    if line.rstrip("\r\n") == "---"), None)
        if end is None:
            raise RuleLoadError(f"invalid YAML front matter in {source}")
        try:
            metadata = yaml.safe_load("".join(lines[1:end]))
        except Exception as exc:
            raise RuleLoadError(f"invalid YAML in {source}") from exc
        if not isinstance(metadata, dict):
            raise RuleLoadError(f"invalid YAML in {source}")
        if "id" not in metadata:
            raise RuleLoadError(f"missing field id in {source}")
        rid = metadata["id"]
        if not isinstance(rid, str) or not _ID_RE.fullmatch(rid):
            raise RuleLoadError(f"invalid rule id in {source}")
        missing = [name for name in _REQUIRED if name not in metadata]
        if missing:
            raise RuleLoadError(f"missing field {missing[0]} in {source}")
        title, language, hint = metadata["title"], metadata["language"], metadata["prompt_hint"]
        if not all(isinstance(value, str) for value in (title, language, hint)):
            raise RuleLoadError(f"invalid field type in {source}")
        domains = metadata["domains"]
        if not isinstance(domains, (list, tuple)) or not all(isinstance(item, str) for item in domains):
            raise RuleLoadError(f"invalid domains in {source}")
        severity = metadata["severity"]
        if not isinstance(severity, str) or severity not in _SEVERITIES:
            raise RuleLoadError(f"invalid severity in {source}")
        deprecated = metadata["deprecated"]
        if not isinstance(deprecated, bool):
            raise RuleLoadError(f"invalid deprecated in {source}")
        return ReviewRule(rid, title, language.lower(), tuple(item.upper() for item in domains),
                          severity, hint, deprecated, "".join(lines[end + 1:]).lstrip("\r\n"), source)
