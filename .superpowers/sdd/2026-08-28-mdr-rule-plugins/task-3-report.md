# Task 3 报告：规则批次与严格响应解析

## 完成内容

- 扩展 `Finding` 的 `severity`、`rule_id` 字段，并扩展 `TraceRecord` 的父 trace、规则 ID、规则集 hash 和 metadata 字段；默认值保持旧 JSON 兼容。
- 新增 `RuleBatch`、`RuleParseResult` 以及按语言的稳定规则批处理。
- 同一语言规则按 rule ID 排序并尽可能合并到单次请求；超出 prompt 限制时按稳定 rule ID 拆批。
- 批次会过滤非目标语言规则，仅保留目标语言和 `common`；diff 及规则长文本会确定性截断，确保渲染 prompt 不超过上限。
- Prompt 包含 JSON-only 输出契约、规则元数据、变更文件和 diff，并对完整构造结果执行 secret 脱敏。
- 解析器仅接受单个 JSON 对象，拒绝 Markdown fence、未知 rule ID、未知文件、非法行号及超长/缺失字段。
- 解析器限制 findings 数量并拒绝反向行号范围，避免异常响应放大资源或产生无效定位。
- 纯 LLM finding 强制 `confidence="advisory"`，severity 始终取自 MDR 规则。

## 验证

```text
pytest -q tests/test_models.py tests/test_rule_review.py  # 10 passed
pytest -q                                                # 66 passed
```

Commit: `c52c277a20dcf5e2f1b1c4572322f370e9787b82`

## 注意事项

- `max_prompt_chars` 过小时会保留规则 ID/元数据并截断 diff、提示和正文；若连固定协议和元数据都无法容纳则抛出明确错误。
- 当前模块提供批次与解析 API，尚未接入主 pipeline 的实际 MDR review 阶段，后续任务负责集成。
