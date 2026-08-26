"""Declaratively registered, in-process review analyzers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import replace
from typing import Callable

from .models import ChangeRequest, Finding
from .security import redact_secrets

Runner = Callable[[ChangeRequest, str], list[Finding]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    runner: Runner
    confidence: str = "advisory"

    def __post_init__(self) -> None:
        if self.confidence not in ("high", "advisory"):
            raise ValueError("confidence must be 'high' or 'advisory'")


class ToolRegistry:
    def __init__(self) -> None:
        self.specs: list[ToolSpec] = []

    def register(self, spec: ToolSpec) -> None:
        if spec.confidence not in ("high", "advisory"):
            raise ValueError("confidence must be 'high' or 'advisory'")
        if any(existing.name == spec.name for existing in self.specs):
            raise ValueError(f"tool already registered: {spec.name}")
        self.specs.append(spec)

    def run_all(self, change_request: ChangeRequest, sanitized_diff: str) -> list[Finding]:
        findings: list[Finding] = []
        sanitized_diff = redact_secrets(sanitized_diff).text
        sanitized_request = replace(change_request, diff=sanitized_diff)
        for spec in self.specs:
            findings.extend(
                replace(finding, confidence=spec.confidence)
                for finding in spec.runner(sanitized_request, sanitized_diff)
            )
        return findings

    @classmethod
    def with_builtins(cls) -> "ToolRegistry":
        registry = cls()
        registry.register(ToolSpec("todo", "Find TODO markers in changed lines", _todo_runner, "high"))
        registry.register(ToolSpec("secret-in-diff", "Find credentials in the original diff", _secret_runner, "high"))
        return registry


def _todo_runner(change_request: ChangeRequest, diff: str) -> list[Finding]:
    findings: list[Finding] = []
    for line_number, line in enumerate(diff.splitlines(), 1):
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+") and re.search(r"\bTODO\b", line, re.IGNORECASE):
            findings.append(Finding("TODO in changed code", "Resolve TODO before merging.", "high", evidence="tool:todo", file_path=None, line_start=line_number, line_end=line_number))
    return findings


def _secret_runner(_change_request: ChangeRequest, sanitized_diff: str) -> list[Finding]:
    if "[REDACTED:" not in sanitized_diff:
        return []
    kinds = ", ".join(sorted(set(re.findall(r"\[REDACTED:([^]]+)\]", sanitized_diff))))
    return [Finding("Secret detected in diff", f"Remove or rotate detected secret ({kinds}).", "high", evidence="tool:secret-in-diff")]
