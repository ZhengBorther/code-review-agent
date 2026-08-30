"""Review 结果的 Markdown 渲染器。"""

from __future__ import annotations

from .pipeline import ReviewResult
from .security import redact_secrets


def _safe(value: str) -> str:
    """对所有渲染出的自由文本统一执行确定性脱敏。"""
    return redact_secrets(value).text


def render_markdown(result: ReviewResult) -> str:
    """渲染 finding、预算状态和 trace 证据，同时避免暴露 secret。"""
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
        raw_kind = str(trace.get("kind", ""))
        trace_kind = _safe(raw_kind)
        trace_details = ""
        if raw_kind == "mdr_batch":
            metadata = trace.get("metadata") or {}
            rule_ids = metadata.get("rule_ids") or []
            trace_details = f"; 规则: {_safe(', '.join(str(item) for item in rule_ids) or '-')}" \
                f"; ruleset_hash: `{_safe(str(trace.get('ruleset_hash', '') or '-'))}`"
        metadata = trace.get("metadata") or {}
        if metadata.get("trace_role") == "diagnostic_diff":
            input_label, output_label = "诊断说明", "诊断原始 diff"
        elif raw_kind == "tool":
            input_label, output_label = "工具输入", "工具输出"
        else:
            input_label, output_label = "Prompt", "模型回复"
        lines.extend([
            f"### `{_safe(str(trace.get('trace_id', '')))}`",
            f"- 类型: {trace_kind}{trace_details}; 工具: {_safe(str(trace.get('tool_name', '') or '-'))}; 模型: {_safe(str(trace.get('model', '') or '-'))}",
            f"- 输入哈希: `{_safe(str(trace.get('input_hash', '')))}`; 成本: ${float(trace.get('cost_usd', 0.0)):.6f}; Prompt tokens: {trace.get('prompt_tokens', 0)}; Completion tokens: {trace.get('completion_tokens', 0)}; 耗时: {trace.get('duration_ms', 0)}ms; 错误: {_safe(str(trace.get('error', '') or '-'))}",
            f"- {input_label}:",
            "```text",
            _safe(str(trace.get("prompt", ""))),
            "```",
            f"- {output_label}:",
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
        f"规则: {_safe(finding.rule_id) or '-'}; 严重度: {_safe(finding.severity) or '-'}; 置信度: {_safe(finding.confidence)}",
        f"证据: {_safe(finding.evidence) or '未提供'}; trace: `{finding.trace_id}`",
        "",
    ]
