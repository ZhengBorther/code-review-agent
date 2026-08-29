import json
from pathlib import Path

from review_agent.eval import evaluate_case, load_expected
from review_agent.models import Finding
from tests.rule_fixtures import make_rule


CASE = Path(__file__).parents[1] / "eval/cases/GO-STYLE-001/many-parameters"


def test_eval_case_is_offline_and_checks_rule_id_and_advisory():
    rule = make_rule()

    def response(_prompt):
        return json.dumps({"findings": [{
            "rule_id": "GO-STYLE-001", "file_path": "main/user.go",
            "line_start": 4, "title": "参数过多", "body": "使用参数结构体",
            "evidence": "七个业务参数",
        }]})

    result = evaluate_case(CASE / "patch.diff", rule, response)
    expected = load_expected(CASE / "expect.json")
    assert result.passed is expected["expect_hit"]
    assert result.actual_rule_ids == (expected["rule_id"],)
    assert result.rejection_count == 0
