# Task 1 完成报告

## 改动

- 新增 `review_agent` 包及版本 `0.1.0`。
- 新增不可变领域模型：`ChangeRequest`、`Finding`、`TraceRecord`、`LLMResponse`、`RunConfig`。
- 所有模型提供显式 `to_dict`/`from_dict`，可直接存入 JSON 字段。
- `Finding` 限制置信度为 `high` 或 `advisory`。
- 新增 `pyproject.toml`，声明 Python 3.11+ 和 pytest 配置。
- 新增模型往返序列化与置信度校验测试。

## 测试

命令：`pytest -q tests/test_models.py`

结果：`3 passed`。

## 风险

- 领域模型字段是首版约定，后续适配器、存储和流水线如需扩展字段应保持默认值以兼容已有 JSON checkpoint。
- `frozen=True` 只保证 dataclass 属性不可重新绑定；`metadata` 等嵌套字典仍是可变对象，调用方应视为只读。
