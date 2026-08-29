import json

from review_agent.diff_languages import LanguageDiff
from review_agent.rule_review import build_rule_batches, parse_rule_response
from tests.rule_fixtures import GO_DIFF, make_rule


def _batch():
    return build_rule_batches(
        LanguageDiff("go", ("internal/user.go",), GO_DIFF, "hash-go"),
        (make_rule(),),
        20_000,
    )[0]


def test_schema_rejects_extra_fields():
    payload = {"findings": [{
        "rule_id": "GO-STYLE-001", "file_path": "internal/user.go",
        "line_start": 1, "title": "标题", "body": "说明文本",
        "evidence": "证据文本", "unexpected": "not allowed",
    }]}
    result = parse_rule_response(json.dumps(payload), _batch())
    assert result.findings == ()
    assert "unexpected" in result.rejections[0]


def test_schema_accepts_file_level_finding_without_lines():
    payload = {"findings": [{
        "rule_id": "GO-STYLE-001", "file_path": "internal/user.go",
        "line_start": None, "line_end": None, "title": "文件问题",
        "body": "文件级说明", "evidence": "整个文件",
    }]}
    result = parse_rule_response(json.dumps(payload), _batch())
    assert result.rejections == ()
    assert result.findings[0].line_start is None


def test_schema_rejects_reversed_line_range():
    payload = {"findings": [{
        "rule_id": "GO-STYLE-001", "file_path": "internal/user.go",
        "line_start": 10, "line_end": 2, "title": "范围错误",
        "body": "说明文本", "evidence": "证据文本",
    }]}
    result = parse_rule_response(json.dumps(payload), _batch())
    assert result.findings == ()
    assert "line_end" in result.rejections[0]
