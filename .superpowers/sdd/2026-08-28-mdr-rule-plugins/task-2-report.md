# Task 2 Report

## Status

Completed.

## Changes

- Added `RuleRegistry` with duplicate-ID rejection, deprecated/disabled filtering, common-rule merging, language filtering, and stable ID ordering.
- Added deterministic SHA-256 `ruleset_hash` based on effective rules and active configuration.
- Added `LanguageDiff` and unified-diff grouping for Go, Python, TypeScript, JavaScript, Java, Rust, and C#.
- Deleted-file sections are skipped; grouped sections are sorted by path and preserve their complete diff text.
- Added registry, hash, grouping, ordering, and deleted-file tests.

## Verification

- `pytest -q tests/test_rule_registry_languages.py`: 5 passed
- `pytest -q`: 58 passed
- `git diff --check`: passed

## Concerns

- Files with extensions outside the supported mapping are returned in an `unknown` group so callers can surface or explicitly handle them.
