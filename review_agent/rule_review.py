"""Batch MDR rules by language and validate their structured LLM output."""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass, replace
from typing import Any

from .diff_languages import LanguageDiff
from .models import Finding
from .rules import ReviewRule
from .security import redact_secrets


@dataclass(frozen=True)
class RuleBatch:
    language: str
    files: tuple[str, ...]
    diff: str
    diff_hash: str
    rules: tuple[ReviewRule, ...]


@dataclass(frozen=True)
class RuleParseResult:
    findings: tuple[Finding, ...] = ()
    rejections: tuple[str, ...] = ()


def build_rule_batches(
    language_diff: LanguageDiff,
    rules: tuple[ReviewRule, ...] | list[ReviewRule],
    max_prompt_chars: int,
) -> tuple[RuleBatch, ...]:
    """Build deterministic same-language batches, splitting only on rule IDs.

    Rules are sorted by ID so retries produce the same prompts and checkpoint
    identities. Common rules should already be included by ``RuleRegistry``.
    """
    if max_prompt_chars <= 0:
        raise ValueError("max_prompt_chars must be positive")
    language = language_diff.language.lower()
    # ``common`` rules are intentionally shared; all other languages are
    # excluded here so callers cannot accidentally create cross-language
    # prompts by passing a broad registry result.
    ordered = tuple(sorted(
        (rule for rule in rules if rule.language.lower() in (language, "common")),
        key=lambda rule: rule.id,
    ))
    if not ordered:
        return ()
    batches: list[RuleBatch] = []
    current: list[ReviewRule] = []
    for rule in ordered:
        candidate = tuple(current + [rule])
        candidate_batch = RuleBatch(language=language, files=language_diff.files,
                                     diff=language_diff.diff, diff_hash=language_diff.diff_hash,
                                     rules=candidate)
        # A multi-rule batch is kept whenever it fits. A single rule may be
        # larger than the limit; retaining it is preferable to dropping it.
        if current and len(build_rule_prompt(candidate_batch)) > max_prompt_chars:
            batches.append(_fit_batch(RuleBatch(language=language, files=language_diff.files,
                                     diff=language_diff.diff, diff_hash=language_diff.diff_hash,
                                     rules=tuple(current)), max_prompt_chars))
            current = [rule]
        else:
            current = list(candidate)
    if current:
        batches.append(_fit_batch(RuleBatch(language=language, files=language_diff.files,
                                 diff=language_diff.diff, diff_hash=language_diff.diff_hash,
                                 rules=tuple(current)), max_prompt_chars))
    return tuple(batches)


def _fit_batch(batch: RuleBatch, limit: int) -> RuleBatch:
    """Deterministically trim variable content until the rendered prompt fits."""
    if len(build_rule_prompt(batch)) <= limit:
        return batch
    # First trim the diff, which is usually the largest component.
    lo, hi = 0, len(batch.diff)
    best = ""
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = replace(batch, diff=batch.diff[:mid])
        if len(build_rule_prompt(candidate)) <= limit:
            best, lo = candidate.diff, mid + 1
        else:
            hi = mid - 1
    fitted = replace(batch, diff=best, diff_hash=hashlib.sha256(best.encode("utf-8")).hexdigest())
    if len(build_rule_prompt(fitted)) <= limit:
        return fitted
    # If rule metadata itself is too large, shorten prose fields while
    # preserving IDs, language, severity and domains.
    variable = [(index, "prompt_hint") for index in range(len(batch.rules))]
    variable += [(index, "body") for index in range(len(batch.rules))]
    for index, field_name in variable:
        rule = fitted.rules[index]
        value = getattr(rule, field_name)
        lo, hi = 0, len(value)
        best_value = ""
        while lo <= hi:
            mid = (lo + hi) // 2
            candidate_rule = replace(rule, **{field_name: value[:mid]})
            candidate_rules = list(fitted.rules)
            candidate_rules[index] = candidate_rule
            candidate = replace(fitted, rules=tuple(candidate_rules))
            if len(build_rule_prompt(candidate)) <= limit:
                best_value, lo = value[:mid], mid + 1
            else:
                hi = mid - 1
        fitted = replace(fitted, rules=tuple(
            replace(item, **{field_name: best_value}) if pos == index else item
            for pos, item in enumerate(fitted.rules)
        ))
        if len(build_rule_prompt(fitted)) <= limit:
            return fitted
    # The fixed contract/identifiers have a minimum representable size. A
    # clear error is safer than returning an over-budget prompt silently.
    raise ValueError(f"max_prompt_chars={limit} is too small for MDR batch metadata")


def build_rule_prompt(batch: RuleBatch) -> str:
    """Render and redact a JSON-only review prompt for a language batch."""
    rules = []
    for rule in batch.rules:
        rules.append({
            "rule_id": rule.id,
            "title": rule.title,
            "severity": rule.severity,
            "domains": list(rule.domains),
            "language": rule.language,
            "prompt_hint": rule.prompt_hint,
            "body": rule.body,
        })
    payload = json.dumps(rules, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    raw = (
        "MDR_RULE_BATCH\n"
        f"LANGUAGE: {batch.language.lower()}\n"
        "JSON only; no Markdown fences/prose. findings fields: rule_id,file_path,line_start,title,body,evidence.\n"
        "RULES:" + payload + "\n"
        "FILES:" + json.dumps(list(batch.files), ensure_ascii=False, separators=(",", ":")) + "\n"
        "DIFF:" + batch.diff
    )
    return redact_secrets(raw).text


_MAX_FIELD = 20_000
_MAX_FINDINGS = 1_000


def _bounded_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    if len(value) > _MAX_FIELD:
        raise ValueError(f"{field} exceeds maximum length")
    return value


def parse_rule_response(text: str, batch: RuleBatch) -> RuleParseResult:
    """Parse a strict JSON response, rejecting untrusted rule/file references."""
    if not isinstance(text, str) or "```" in text:
        return RuleParseResult(rejections=("response must be JSON without Markdown fences",))
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return RuleParseResult(rejections=("response is not valid JSON",))
    if not isinstance(payload, dict) or set(payload) - {"findings"} or not isinstance(payload.get("findings"), list):
        return RuleParseResult(rejections=("response must be an object with a findings array",))
    if len(payload["findings"]) > _MAX_FINDINGS:
        return RuleParseResult(rejections=(f"findings exceeds maximum of {_MAX_FINDINGS}",))
    rule_map = {rule.id: rule for rule in batch.rules}
    files = set(batch.files)
    findings: list[Finding] = []
    rejections: list[str] = []
    for index, item in enumerate(payload["findings"]):
        try:
            if not isinstance(item, dict):
                raise ValueError("finding must be an object")
            rule_id = _bounded_string(item.get("rule_id"), "rule_id")
            file_path = _bounded_string(item.get("file_path"), "file_path")
            if rule_id not in rule_map:
                raise ValueError(f"unknown rule_id: {rule_id}")
            if file_path not in files:
                raise ValueError(f"file_path is not in batch: {file_path}")
            line_start = item.get("line_start")
            if line_start is not None and (isinstance(line_start, bool) or not isinstance(line_start, int) or line_start <= 0):
                raise ValueError("line_start must be a positive integer")
            line_end = item.get("line_end")
            if line_end is not None and (isinstance(line_end, bool) or not isinstance(line_end, int) or line_end <= 0):
                raise ValueError("line_end must be a positive integer")
            if line_start is None and line_end is not None:
                raise ValueError("line_end requires line_start")
            if line_end is not None and line_end < line_start:
                raise ValueError("line_end must not precede line_start")
            title = _bounded_string(item.get("title"), "title")
            body = _bounded_string(item.get("body"), "body")
            evidence = _bounded_string(item.get("evidence"), "evidence")
            rule = rule_map[rule_id]
            findings.append(Finding(title=title, body=body, confidence="advisory",
                                    evidence=evidence, file_path=file_path,
                                    line_start=line_start, line_end=line_end,
                                    severity=rule.severity, rule_id=rule.id))
        except ValueError as exc:
            rejections.append(f"finding[{index}]: {exc}")
    return RuleParseResult(findings=tuple(findings), rejections=tuple(rejections))
