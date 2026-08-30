import pytest
from pathlib import Path

from review_agent.config import load_rules_config
from review_agent.rules import MdrRuleLoader, RuleLoadError
from tests.rule_fixtures import VALID_GO_RULE


def test_repository_contains_python_sensitive_output_rule():
    rules_dir = Path(__file__).parents[1] / "rules"
    loaded = MdrRuleLoader().load(rules_dir)
    rule = next(item for item in loaded if item.id == "PY-SEC-001")
    assert rule.language == "python"
    assert rule.severity == "error"
    assert "password" in rule.prompt_hint.lower()
    assert "print" in rule.prompt_hint.lower()

def test_loads_valid_mdr_and_merges_toml_with_cli_directories(tmp_path):
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "GO-STYLE-001.mdr").write_text(VALID_GO_RULE, encoding="utf-8")
    config_file = tmp_path / "review-agent.toml"
    config_file.write_text('[rules]\ndirectories = ["rules"]\nenabled_languages = ["go"]\n'
                           'disabled_rules = ["GO-OLD-001"]\n', encoding="utf-8")
    config = load_rules_config(config_file, [tmp_path / "extra"])
    loaded = MdrRuleLoader().load(config.directories[0])
    assert config.directories == (rules.resolve(), (tmp_path / "extra").resolve())
    assert loaded[0].id == "GO-STYLE-001"
    assert loaded[0].language == "go"
    assert loaded[0].source == "GO-STYLE-001.mdr"

@pytest.mark.parametrize("content,error", [("---\nid: bad id\n---\n", "invalid rule id"),
    (VALID_GO_RULE.replace("severity: warning", "severity: critical"), "invalid severity"),
    ("---\n!!python/object:os.system ['echo unsafe']\n---\n", "invalid YAML")])
def test_rejects_invalid_mdr(content, error, tmp_path):
    (tmp_path / "bad.mdr").write_text(content, encoding="utf-8")
    with pytest.raises(RuleLoadError, match=error):
        MdrRuleLoader().load(tmp_path)

def test_rejects_invalid_encoding(tmp_path):
    (tmp_path / "bad.mdr").write_bytes(b"---\nid: GO-1\n\xff")
    with pytest.raises(RuleLoadError, match="UTF-8"):
        MdrRuleLoader().load(tmp_path)

def test_rejects_oversized_rule(tmp_path):
    (tmp_path / "large.mdr").write_text(VALID_GO_RULE, encoding="utf-8")
    with pytest.raises(RuleLoadError, match="too large"):
        MdrRuleLoader(max_file_bytes=10).load(tmp_path)

def test_rejects_missing_required_field(tmp_path):
    (tmp_path / "missing.mdr").write_text("---\nid: GO-STYLE-001\n---\n", encoding="utf-8")
    with pytest.raises(RuleLoadError, match="missing field title"):
        MdrRuleLoader().load(tmp_path)

def test_rejects_wrong_field_types(tmp_path):
    content = VALID_GO_RULE.replace("domains: [STYLE]", "domains: STYLE")
    (tmp_path / "wrong.mdr").write_text(content, encoding="utf-8")
    with pytest.raises(RuleLoadError, match="invalid domains"):
        MdrRuleLoader().load(tmp_path)

def test_front_matter_fence_is_line_based_and_body_may_contain_separator(tmp_path):
    content = VALID_GO_RULE.replace(
        "  检查新增/修改的函数签名：当参数数量大于4个时必须封装。",
        "  检查新增/修改的函数签名。\n  示例分隔符：---\n  仍属于提示内容。",
    )
    content += "\n正文中的分隔符 --- 不应截断正文。\n"
    path = tmp_path / "rule.mdr"
    path.write_text(content, encoding="utf-8")
    rule = MdrRuleLoader().load(tmp_path)[0]
    assert "示例分隔符：---" in rule.prompt_hint
    assert "正文中的分隔符 ---" in rule.body

def test_rejects_symlink_rule_file(tmp_path):
    outside = tmp_path / "outside.mdr"
    outside.write_text(VALID_GO_RULE, encoding="utf-8")
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "linked.mdr").symlink_to(outside)
    with pytest.raises(RuleLoadError, match="symlink"):
        MdrRuleLoader().load(rules)
