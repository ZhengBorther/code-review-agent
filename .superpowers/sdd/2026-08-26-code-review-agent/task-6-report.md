# Task 6 实现报告

## 状态

完成。

## 变更

- 新增 `review_agent/cli.py` 和 `review_agent/__main__.py`，支持 `review` 命令及 diff、输出、状态目录、预算、模型、OneAPI、离线参数。
- 新增离线 unified diff fixture 与 CLI E2E 测试。
- 新增中文 README，记录 OneAPI 配置、恢复机制、预算降级和安全边界。

## 验证

- `pytest -q`: 34 passed
- `python3 -m review_agent review ... --offline`: 返回 0，报告包含高置信度/建议分级及 trace 附录。
- `python3 -m review_agent --help`: 返回 0。
- `git diff --check`: 通过。

注意：当前环境中的 `python` 命令指向 Python 2.7；项目要求 Python >=3.11，因此文档使用 `python3` 命令。
