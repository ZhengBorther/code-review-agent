# Task 3 完成报告

## 状态

已完成。

## 变更

- `review_agent/adapters.py`：实现 `LocalDiffAdapter`，仅接受 `local://` URL，从显式 diff 文件读取内容；拒绝网络 URL。
- `review_agent/security.py`：实现私钥、常见 key/token 前缀、密码赋值和高熵引号值检测；使用稳定 `[REDACTED:<kind>]` 占位符并返回匹配元数据。
- `review_agent/tools.py`：实现声明式 `ToolSpec`/`ToolRegistry`，只运行显式注册的进程内 runner；内置 TODO 和 secret-in-diff 规则。
- `tests/test_security_tools.py`：覆盖脱敏、适配器 URL 安全、工具注册与内置规则。

## 验证

- `pytest -q tests/test_security_tools.py`：6 passed
- `pytest -q`：15 passed
- `git diff --check`：通过

## Commit

初始提交：`f6adcb2` (`feat: add safe adapters redaction and tool registry`)

修复提交：`02732bb`，补强 runner 脱敏边界、平台 token 规则和注册时 confidence 校验。

最终补丁：待提交，按 `ToolSpec.confidence` 规范化 runner 返回的每条 finding。

## Concerns

- 当前适配器是离线本地实现；GitHub/GitLab 网络适配器需在后续任务中实现。
- secret 规则是启发式检测，不能替代专用 secret scanner；工具 runner 只接收脱敏后的 `ChangeRequest` 和 diff，发送 LLM 前仍必须调用 `redact_secrets`。

## 修复验证

- `pytest -q tests/test_security_tools.py`：9 passed
- `pytest -q`：18 passed

最终补丁验证：

- `pytest -q tests/test_security_tools.py`：10 passed
- `pytest -q`：19 passed
