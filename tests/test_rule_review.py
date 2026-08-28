import json

from review_agent.diff_languages import LanguageDiff
from review_agent.rule_review import build_rule_batches, build_rule_prompt, parse_rule_response
from tests.rule_fixtures import GO_DIFF, make_rule


def test_multiple_go_rules_share_one_batch_and_prompt_is_redacted():
    language_diff = LanguageDiff(
        language="go", files=("internal/user.go",),
        diff=GO_DIFF + "+token = 'sk-test-secret-123456789'\n",
        diff_hash="hash-go")
    batches = build_rule_batches(
        language_diff,
        (make_rule(), make_rule("COMMON-SECURITY-001", "common")),
        20_000)
    prompt = build_rule_prompt(batches[0])
    assert len(batches) == 1
    assert "GO-STYLE-001" in prompt and "COMMON-SECURITY-001" in prompt
    assert "sk-test-secret" not in prompt and "[REDACTED:" in prompt
    assert prompt.startswith("MDR_RULE_BATCH\nLANGUAGE: go")


def test_batches_split_rules_by_stable_id_when_prompt_is_oversized():
    language_diff = LanguageDiff("go", ("a.go",), GO_DIFF, "hash-go")
    rules = (make_rule("GO-Z", prompt_hint="z" * 100), make_rule("GO-A", prompt_hint="a" * 100))
    batches = build_rule_batches(language_diff, rules, 300)
    assert len(batches) == 2
    assert [batch.rules[0].id for batch in batches] == ["GO-A", "GO-Z"]
    assert all(len(build_rule_prompt(batch)) <= 300 for batch in batches)


def test_batches_filter_rules_to_language_and_common_only():
    language_diff = LanguageDiff("go", ("a.go",), GO_DIFF, "hash-go")
    batches = build_rule_batches(language_diff, (
        make_rule("PY-STYLE-001", "python"),
        make_rule("COMMON-SECURITY-001", "common"),
        make_rule("GO-STYLE-001", "go"),
    ), 20_000)
    assert [rule.id for rule in batches[0].rules] == ["COMMON-SECURITY-001", "GO-STYLE-001"]


def test_parser_accepts_known_rule_and_forces_advisory():
    go_batch = build_rule_batches(
        LanguageDiff("go", ("internal/user.go",), GO_DIFF, "hash-go"),
        (make_rule(),), 20_000)[0]
    parsed = parse_rule_response(json.dumps({"findings": [{
        "rule_id": "GO-STYLE-001", "file_path": "internal/user.go",
        "line_start": 12, "title": "too many parameters",
        "body": "use CreateUserParams", "evidence": "five parameters",
        "confidence": "high"}]}), go_batch)
    assert parsed.findings[0].confidence == "advisory"
    assert parsed.findings[0].severity == "warning"
    assert parsed.findings[0].rule_id == "GO-STYLE-001"


def test_parser_rejects_unknown_rule_and_file():
    go_batch = build_rule_batches(
        LanguageDiff("go", ("internal/user.go",), GO_DIFF, "hash-go"),
        (make_rule(),), 20_000)[0]
    unknown = json.dumps({"findings": [
        {"rule_id": "GO-UNKNOWN-001", "file_path": "internal/user.go",
         "line_start": 1, "title": "x", "body": "x", "evidence": "x"},
        {"rule_id": "GO-STYLE-001", "file_path": "other.go",
         "line_start": 1, "title": "x", "body": "x", "evidence": "x"},
    ]})
    parsed = parse_rule_response(unknown, go_batch)
    assert parsed.findings == ()
    assert len(parsed.rejections) == 2


def test_parser_rejects_markdown_fences_and_invalid_shape():
    batch = build_rule_batches(LanguageDiff("go", ("a.go",), GO_DIFF, "h"), (make_rule(),), 20_000)[0]
    fenced = "```json\n{" + '"findings": []' + "}\n```"
    assert parse_rule_response(fenced, batch).findings == ()
    assert parse_rule_response("[]", batch).rejections


def test_parser_rejects_unbounded_findings_and_reversed_location():
    batch = build_rule_batches(LanguageDiff("go", ("a.go",), GO_DIFF, "h"), (make_rule(),), 20_000)[0]
    many = parse_rule_response(json.dumps({"findings": [{}] * 1001}), batch)
    assert many.rejections
    reversed_lines = {"findings": [{"rule_id": "GO-STYLE-001", "file_path": "internal/user.go",
        "line_start": 4, "line_end": 2, "title": "x", "body": "x", "evidence": "x"}]}
    parsed = parse_rule_response(json.dumps(reversed_lines), batch)
    assert parsed.findings == () and parsed.rejections


def test_parser_accepts_file_level_finding_without_line_start():
    batch = build_rule_batches(LanguageDiff("go", ("a.go",), GO_DIFF, "h"), (make_rule(),), 20_000)[0]
    parsed = parse_rule_response(json.dumps({"findings": [{
        "rule_id": "GO-STYLE-001", "file_path": "a.go", "line_start": None,
        "line_end": None, "title": "file issue", "body": "review file", "evidence": "file evidence"
    }]}), batch)
    assert len(parsed.findings) == 1
    assert parsed.findings[0].line_start is None


def test_truncated_batch_hash_matches_actual_diff():
    language_diff = LanguageDiff("go", ("a.go",), GO_DIFF, "original-hash")
    batch = build_rule_batches(language_diff, (make_rule(),), 300)[0]
    import hashlib
    assert batch.diff_hash == hashlib.sha256(batch.diff.encode()).hexdigest()
