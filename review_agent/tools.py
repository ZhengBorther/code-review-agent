"""Declaratively registered, in-process review analyzers."""

from __future__ import annotations

import re
from dataclasses import dataclass
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
        if any(existing.name == spec.name for existing in self.specs):
            raise ValueError(f"tool already registered: {spec.name}")
        self.specs.append(spec)

    def run_all(self, change_request: ChangeRequest, sanitized_diff: str) -> list[Finding]:
        findings: list[Finding] = []
        for spec in self.specs:
            findings.extend(spec.runner(change_request, sanitized_diff))
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


def _secret_runner(change_request: ChangeRequest, _sanitized_diff: str) -> list[Finding]:
    result = redact_secrets(change_request.diff)
    if not result.matches:
        return []
    kinds = ", ".join(sorted({match.kind for match in result.matches}))
    return [Finding("Secret detected in diff", f"Remove or rotate detected secret ({kinds}).", "high", evidence="tool:secret-in-diff")]
