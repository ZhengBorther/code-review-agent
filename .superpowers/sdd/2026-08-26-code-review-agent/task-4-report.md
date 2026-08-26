# Task 4 报告：OneAPI 客户端与预算策略

## 完成内容

- 新增 `OpenAICompatibleClient`，通过标准库 `urllib.request` 调用 OpenAI chat-completions 兼容接口（包括 OneAPI），解析文本与 usage。
- 新增 `DeterministicClient`，用于离线、可重复的评审运行。
- 新增 `BudgetController` 与 `Decision`，严格保证选中模型估算成本不超过剩余预算；fallback 仍超预算时禁用 LLM。通过 `allow_truncate=True` 可获得带 `max_tokens`、`max_chars` 和 `estimated_tokens` 的可执行截断决策。
- 成本估算使用按千 token 的模型费率，响应中的 prompt/completion usage 会写入 `LLMResponse.cost_usd`。服务端缺失 usage 时按文本长度估算并将 `usage_known=False`，避免被误计为零成本。

## 验证

```text
pytest -q tests/test_llm_budget.py  # 3 passed
pytest -q                         # 22 passed
```

## 注意事项

- 客户端不会自行记录或持久化 prompt；调用方必须在请求前完成脱敏，符合安全边界。
- `review` 支持 `max_chars`/`max_tokens`，OneAPI 请求会实际截断 prompt 并传递 token 上限。
- 实际成本超出剩余预算时会硬封顶并锁定控制器，后续调用全部拒绝。
- 默认费率为保守估算值，可通过 `BudgetController(pricing=...)` 覆盖。
