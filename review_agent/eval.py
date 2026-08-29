"""离线 MDR 评测辅助，不访问网络，也不执行 fixture 中的代码。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .diff_languages import split_diff_by_language
from .models import Finding
from .rule_review import build_rule_batches, build_rule_prompt, parse_rule_response
from .rules import ReviewRule


@dataclass(frozen=True)
class EvalResult:
    """一条 eval case 的确定性结果。"""

    case: str
    expected_rule_id: str
    actual_rule_ids: tuple[str, ...]
    passed: bool
    rejection_count: int


def evaluate_case(
    diff_path: str | Path,
    rule: ReviewRule,
    response_factory: Callable[[str], str],
) -> EvalResult:
    """用注入的离线响应评估规则，不创建真实网络客户端。"""
    path = Path(diff_path)
    diff = path.read_text(encoding="utf-8")
    language_diff = next(
        item for item in split_diff_by_language(diff)
        if item.language == rule.language
    )
    batch = build_rule_batches(language_diff, (rule,), max_prompt_chars=20_000)[0]
    response = parse_rule_response(response_factory(build_rule_prompt(batch)), batch)
    actual = tuple(sorted(item.rule_id for item in response.findings))
    return EvalResult(
        case=str(path),
        expected_rule_id=rule.id,
        actual_rule_ids=actual,
        passed=rule.id in actual,
        rejection_count=len(response.rejections),
    )


def load_expected(path: str | Path) -> dict[str, Any]:
    """读取 eval JSON 期望，不把其内容当作代码执行。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))
