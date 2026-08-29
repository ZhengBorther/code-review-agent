# MDR Final Recovery Fixes

日期：2026-08-29

本次修复针对 `b7ae1ef` final signoff 中的 Important 问题，并补充了相关回归测试。

## 修复内容

- 规则批次被 supersede 时，通过 SQLite 事务同时标记 checkpoint 并释放在途 reservation，避免旧规则永久占用预算。
- reservation 结算时原子保存模型响应、解析后的 findings、prompt 和 usage。若结算后 trace/checkpoint 写入前崩溃，恢复流程从已结算 reservation 重建 batch/finding trace 与 findings，不再静默生成空结果。
- unknown-language 诊断 trace 和 degradation 纳入 render input hash，确保新的诊断不会复用旧 Markdown。
- `enabled_languages` 仅限制具体语言；`common` 规则仍适用于 unknown 变更。
- CLI 保留 MDR 相对 source 标识；重复规则错误仍展示两个授权目录的绝对路径。
- 缺少 `+++` 的 binary/malformed diff section 从 `diff --git` 头恢复路径并进入 unknown 诊断。
- MDR trace 的 input hash 现在对应最终实际发送的（可能被 `max_chars` 截断的）prompt，并保留原始批次 diff hash 元数据。

## 验证

```text
pytest -q           95 passed
git diff --check    passed
```

新增测试覆盖 supersede reservation 清理、结算后 trace 崩溃恢复、unknown render identity、语言白名单下 common 规则，以及 binary diff 可观测性。
