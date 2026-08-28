import pytest

from review_agent.config import RulesConfig
from review_agent.diff_languages import split_diff_by_language
from review_agent.rules import ReviewRule, RuleLoadError, RuleRegistry


def make_rule(rule_id="GO-STYLE-001", language="go", deprecated=False):
    return ReviewRule(
        id=rule_id, title="Rule title", language=language,
        domains=("STYLE",), severity="warning", prompt_hint="Check code",
        deprecated=deprecated, body="# Rule", source=f"{rule_id}.mdr",
    )


def test_registry_filters_disabled_deprecated_and_merges_common_rules():
    registry = RuleRegistry(RulesConfig(
        enabled_languages=("go",), disabled_rules=frozenset({"GO-OFF-001"})))
    registry.register(make_rule("COMMON-SEC-001", "common"))
    registry.register(make_rule("GO-STYLE-001", "go"))
    registry.register(make_rule("GO-OFF-001", "go"))
    registry.register(make_rule("GO-OLD-001", "go", deprecated=True))
    assert [item.id for item in registry.applicable("go")] == [
        "COMMON-SEC-001", "GO-STYLE-001"]


def test_registry_rejects_duplicate_ids():
    registry = RuleRegistry(RulesConfig())
    registry.register(make_rule("GO-STYLE-001"))
    with pytest.raises(RuleLoadError, match="duplicate rule id"):
        registry.register(make_rule("GO-STYLE-001", "python"))


def test_ruleset_hash_is_stable_and_changes_with_effective_rules():
    first = RuleRegistry(RulesConfig(enabled_languages=("go",)))
    first.register(make_rule("GO-STYLE-002"))
    first.register(make_rule("GO-STYLE-001"))
    second = RuleRegistry(RulesConfig(enabled_languages=("go",)))
    second.register(make_rule("GO-STYLE-001"))
    second.register(make_rule("GO-STYLE-002"))
    assert first.ruleset_hash("go") == second.ruleset_hash("go")
    second.register(make_rule("COMMON-SEC-001", "common"))
    assert first.ruleset_hash("go") != second.ruleset_hash("go")


def test_splits_unified_diff_by_language_and_preserves_sections():
    mixed_diff = (
        "diff --git a/internal/user.go b/internal/user.go\n"
        "--- a/internal/user.go\n+++ b/internal/user.go\n+func CreateUser() {}\n"
        "diff --git a/service.py b/service.py\n"
        "--- a/service.py\n+++ b/service.py\n+def create_user(): pass\n"
    )
    grouped = {item.language: item for item in split_diff_by_language(mixed_diff)}
    assert grouped["go"].files == ("internal/user.go",)
    assert "+func CreateUser" in grouped["go"].diff
    assert grouped["python"].files == ("service.py",)
    assert "service.py" not in grouped["go"].diff


def test_deleted_files_are_skipped_and_sections_are_stably_sorted():
    diff = (
        "diff --git a/z.go b/z.go\n--- a/z.go\n+++ b/z.go\n+z\n"
        "diff --git a/deleted.py b/deleted.py\n--- a/deleted.py\n+++ /dev/null\n-x\n"
        "diff --git a/a.go b/a.go\n--- a/a.go\n+++ b/a.go\n+a\n"
    )
    grouped = split_diff_by_language(diff)
    assert grouped[0].language == "go"
    assert grouped[0].files == ("a.go", "z.go")
    assert len(grouped) == 1
