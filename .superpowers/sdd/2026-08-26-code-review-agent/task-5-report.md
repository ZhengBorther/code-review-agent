# Task 5 实现报告

## 状态

已完成，commit：`8c7a17f`（`feat: add resumable review pipeline and markdown report`）。

## 实现内容

- `ReviewPipeline` 实现 `fetch -> sanitize -> tools -> review -> render` 五阶段，并在每阶段保存 SQLite checkpoint；传入已有 `run_id` 时复用成功阶段。
- 原始 diff 仅记录哈希供审计，脱敏 diff 才进入工具和 LLM prompt；trace 写入边界再次脱敏 prompt、回复和错误。
- 每个工具 finding 和 LLM finding 都生成唯一 `trace-*` ID；trace 包含输入哈希、prompt、回复、模型、用量及成本。
- 通过 `BudgetController` 执行 fallback、上下文截断、禁用 LLM 的降级顺序，并在报告记录降级原因和累计成本。
- LLM 调用前持久化 `review_reservation`（token、reserved_usd、model、输入哈希）；异常重启检测未完成 reservation 后跳过重复调用。provider 实际成本超预算时只写审计 trace 和降级 finding，账面成本封顶，不采纳模型回复。
- Markdown 报告按“可直接采纳”和“仅供参考”分组，附带元数据、预算及 trace 附录。

## 验证

`pytest -q`：32 passed。

## 注意事项

当前工具 runner 仍由已有 `ToolRegistry` 提供，远程 GitHub/GitLab 发布评论保持未实现；CLI 由 Task 6 接入。
