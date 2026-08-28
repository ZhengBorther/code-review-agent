# Task 4 Report

## 完成内容

- `ReviewPipeline` 新增兼容的 `rules: RuleRegistry | None` 参数。
- MDR 规则按变更语言批量执行，同语言规则共享一次 LLM 请求，执行顺序早于通用 review。
- 每个批次使用 `rules:<language>:<batch_index>` checkpoint，载荷包含规则集哈希、diff 哈希、finding 和 trace ID。
- LLM 请求前写入 `:reservation` checkpoint，并复用现有 SQLite 原子预算 reservation/settle；恢复时跳过匹配的未决 reservation，避免重复计费。
- 新增 `mdr_batch` 和 `mdr_finding` trace，finding trace 通过 `parent_trace_id` 关联批次。
- 报告展示 MDR 规则 ID、严重度、置信度，以及批次规则列表和 `ruleset_hash`。
- render checkpoint 通过活动 finding/batch trace 输入哈希失效，规则变更不会复用旧报告。
- 恢复未决 MDR reservation 时释放 SQLite 预留；成功响应与规则 checkpoint 之间崩溃时从 reservation checkpoint 重建 findings；unknown language 变更写入诊断 trace 和降级摘要。
- reservation 采用 `pending -> in_flight -> completed` 协议；恢复按 token 精确查询并释放匹配 DB reservation。旧的无 checkpoint orphan 只记录人工恢复提示，不按时间自动释放。
- `unknown` 语言变更在存在 `common` 规则时照常执行 MDR；仅无适用 common 规则时生成诊断 trace。`pending + DB in_flight` 和 completed 缺失结果均生成 recovery trace/degradation。

## 验证

```text
pytest -q
77 passed
```

新增测试覆盖按语言单次调用、规则哈希复用/失效、语言隔离和 finding trace 关系。
