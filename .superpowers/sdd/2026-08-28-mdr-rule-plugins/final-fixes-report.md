# MDR Rule Plugins Final Fixes

日期：2026-08-28

## 修复内容

- 规则集合变化时，将失效的 MDR batch/reservation checkpoint 标记为 `superseded`；Markdown 只展示当前有效 batch/finding trace，并因 active 输入 hash 变化重新生成报告。
- `RunConfig` 持久化规则目录、启停配置、规则快照及自托管 GitLab 主机。仅使用 `--run-id` 恢复时从快照重建规则集，避免静默跳过 MDR。
- 规则响应允许 `line_start`/`line_end` 均为空的文件级 finding；非空位置仍校验正整数及范围关系。
- 重复 rule ID 错误包含冲突双方来源文件。
- 截断后的 MDR batch 使用实际发送 diff 的 SHA-256 hash。
- CLI 支持通过 `--gitlab-host` 或 `GITLAB_ALLOWED_HOSTS` 显式授权自托管 GitLab，token 仅发送到授权主机。

## 验证

严格按 TDD 添加回归测试，先确认新增测试失败，再实现修复。完整测试结果：`pytest -q` -> **90 passed**。
