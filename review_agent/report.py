"""Markdown renderer for review results."""

from __future__ import annotations

from .pipeline import ReviewResult
from .security import redact_secrets


def _safe(value: str) -> str:
    return redact_secrets(value).text


def render_markdown(result: ReviewResult) -> str:
    high = [item for item in result.findings if item.confidence == "high"]
    advisory = [item for item in result.findings if item.confidence == "advisory"]
    lines = [
        "# Code Review",
        "",
        f"- Run ID: `{result.run_id}`",
        f"- URL: `{_safe(result.request.url)}`",
        f"- 预算: ${result.cost_usd:.4f} / ${result.budget_usd:.4f}",
        f"- Findings: {len(result.findings)} (高置信度 {len(high)}, 建议 {len(advisory)})",
        "",
    ]
    if result.degradations:
        lines.extend(["## 降级说明", "", "、".join(dict.fromkeys(result.degradations)), ""])
    lines.extend(["## 高置信度：可直接采纳", ""])
    if high:
        for finding in high:
            lines.extend(_finding_lines(finding))
    else:
        lines.append("暂无。\n")
    lines.extend(["## 建议：仅供参考", ""])
    if advisory:
        for finding in advisory:
            lines.extend(_finding_lines(finding))
    else:
        lines.append("暂无。\n")
    lines.extend(["## Trace 附录", ""])
    for trace in result.traces:
        lines.extend([
            f"### `{_safe(str(trace.get('trace_id', '')))}`",
            f"- 类型: {_safe(str(trace.get('kind', '')))}; 工具: {_safe(str(trace.get('tool_name', '') or '-'))}; 模型: {_safe(str(trace.get('model', '') or '-'))}",
            f"- 输入哈希: `{_safe(str(trace.get('input_hash', '')))}`; 成本: ${float(trace.get('cost_usd', 0.0)):.6f}; Prompt tokens: {trace.get('prompt_tokens', 0)}; Completion tokens: {trace.get('completion_tokens', 0)}; 耗时: {trace.get('duration_ms', 0)}ms; 错误: {_safe(str(trace.get('error', '') or '-'))}",
            "- Prompt:",
            "```text",
            _safe(str(trace.get("prompt", ""))),
            "```",
            "- 回复:",
            "```text",
            _safe(str(trace.get("response", ""))),
            "```",
            "",
        ])
    return "\n".join(lines)


def _finding_lines(finding) -> list[str]:
    location = ""
    if finding.file_path:
        location = f" ({_safe(finding.file_path)}"
        if finding.line_start is not None:
            location += f":{finding.line_start}"
        location += ")"
    return [
        f"### {_safe(finding.title)}{location}",
        "",
        _safe(finding.body),
        "",
        f"证据: {_safe(finding.evidence) or '未提供'}; trace: `{finding.trace_id}`",
        "",
    ]
