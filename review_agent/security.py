"""Secret detection and deterministic redaction before LLM calls."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SecretMatch:
    kind: str
    replacement: str
    start: int
    end: int


@dataclass(frozen=True)
class RedactionResult:
    text: str
    matches: tuple[SecretMatch, ...] = ()


_PRIVATE_KEY = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.DOTALL)
_API_KEY = re.compile(r"\b(?:sk|pk|AKIA)[-_][A-Za-z0-9][A-Za-z0-9_-]{12,}")
_PLATFORM_TOKEN = re.compile(r"\b(?:ghp_|gho_|glpat-|xoxb-)[A-Za-z0-9_-]{16,}")
_BEARER = re.compile(r"\bBearer[ \t]+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE)
_TOKEN = re.compile(r"\b(?:token|api[_-]?key|secret)[ \t]*[=:][ \t]*(['\"])([^'\"]{12,})\1", re.IGNORECASE)
_PASSWORD = re.compile(r"\bpassword[ \t]*[=:][ \t]*(['\"])([^'\"]+)\1", re.IGNORECASE)
_UNQUOTED_TOKEN = re.compile(r"\b(?:token|api[_-]?key|secret)[ \t]*[=:][ \t]*([A-Za-z0-9._~+/=-]{12,})", re.IGNORECASE)
_UNQUOTED_PASSWORD = re.compile(r"\bpassword[ \t]*[=:][ \t]*([^\s,;]+)", re.IGNORECASE)


def _replacement(kind: str) -> str:
    return f"[REDACTED:{kind}]"


def redact_secrets(text: str) -> RedactionResult:
    """Redact common credentials while preserving deterministic match metadata."""
    matches: list[SecretMatch] = []
    spans: list[tuple[int, int, str]] = []

    for pattern, kind in ((_PRIVATE_KEY, "private_key"), (_API_KEY, "api_key"), (_PLATFORM_TOKEN, "token"), (_BEARER, "token")):
        for match in pattern.finditer(text):
            spans.append((match.start(), match.end(), kind))
    for pattern, kind in ((_PASSWORD, "password"), (_TOKEN, "token")):
        for match in pattern.finditer(text):
            if match.group(2).startswith("[REDACTED:"):
                continue
            spans.append((match.start(2), match.end(2), kind))
    for pattern, kind in ((_UNQUOTED_PASSWORD, "password"), (_UNQUOTED_TOKEN, "token")):
        for match in pattern.finditer(text):
            if match.group(1).startswith("[REDACTED:"):
                continue
            spans.append((match.start(1), match.end(1), kind))

    # Long quoted assignments are likely credentials when no specific name exists.
    for match in re.finditer(r"(['\"])([^'\"\n]{24,})\1", text):
        value = match.group(2)
        if len(set(value)) >= 8 and not any(start <= match.start(2) < end for start, end, _ in spans):
            spans.append((match.start(2), match.end(2), "high_entropy"))

    # Prefer larger matches and make overlapping patterns deterministic.
    selected: list[tuple[int, int, str]] = []
    for start, end, kind in sorted(spans, key=lambda item: (item[0], -(item[1] - item[0]))):
        if any(start < other_end and end > other_start for other_start, other_end, _ in selected):
            continue
        selected.append((start, end, kind))
    selected.sort()
    output: list[str] = []
    cursor = 0
    for start, end, kind in selected:
        output.append(text[cursor:start])
        replacement = _replacement(kind)
        output.append(replacement)
        matches.append(SecretMatch(kind, replacement, start, end))
        cursor = end
    output.append(text[cursor:])
    return RedactionResult("".join(output), tuple(matches))
