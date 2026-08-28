import pytest

from review_agent.config import load_rules_config
from review_agent.rules import MdrRuleLoader, RuleLoadError
from tests.rule_fixtures import VALID_GO_RULE

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
