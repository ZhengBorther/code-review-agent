# Task 4 报告：OneAPI 客户端与预算策略

## 完成内容

- 新增 `OpenAICompatibleClient`，通过标准库 `urllib.request` 调用 OpenAI chat-completions 兼容接口（包括 OneAPI），解析文本与 usage。
- 新增 `DeterministicClient`，用于离线、可重复的评审运行。
- 新增 `BudgetController` 与 `Decision`，记录成本并实现主模型 -> fallback 模型 -> 超预算禁用的策略；支持显式截断决策字段。
- 成本估算使用按千 token 的模型费率，响应中的 prompt/completion usage 会写入 `LLMResponse.cost_usd`。

## 验证

```text
pytest -q tests/test_llm_budget.py  # 3 passed
pytest -q                         # 22 passed
```

## 注意事项

- 客户端不会自行记录或持久化 prompt；调用方必须在请求前完成脱敏，符合安全边界。
- 默认费率为保守估算值，可通过 `BudgetController(pricing=...)` 覆盖。
