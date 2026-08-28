# Task 1 Report

## Status

Completed.

## Changes

- Added immutable `ReviewRule` and `RuleLoadError` models.
- Added safe MDR loader using YAML `safe_load` only.
- Added required-field, type, ID, severity, UTF-8, and file-size validation.
- Normalized language to lowercase and domains to uppercase.
- Skipped deprecated rules and stored only authorized-directory-relative source paths.
- Added `RulesConfig` and TOML loading with TOML-relative and CLI-relative directory resolution.
- Added PyYAML runtime dependency.

## Verification

- `pytest -q tests/test_rule_loading.py`: 8 passed
- `pytest -q`: 51 passed
- `git diff --check`: passed

## Reviewer Follow-up

- Front matter fences are now recognized only as exact standalone lines at the file start and closing boundary; separators in YAML block scalars or rule bodies are preserved.
- Symlinked MDR files are rejected, and every resolved file path is checked to remain beneath the explicitly authorized root.
- Configured rule directories are de-duplicated while preserving declaration order.

## Concerns

- The environment contains an older PyYAML release that references removed Python 3.13 `collections` aliases; the loader includes a compatibility shim. Installing the declared `PyYAML>=6,<7` dependency avoids relying on that legacy behavior.
- Rule execution, language grouping, and pipeline integration are intentionally deferred to later tasks.
