# Code Review Agent Whole-Branch Review

审查基线：`9058dc6..4f72ac3` 及实现计划 `docs/superpowers/plans/2026-08-26-code-review-agent.md`。

验证：`pytest -q` -> `36 passed`。另外用两个不同的 diff 文件、同一 `--state-dir` 连续运行 CLI，确认第二次运行复用了第一次的结果。

## 结论

当前版本**不可交付**为题目要求的 Code Review Agent。离线 diff -> Markdown 的演示链路可运行，checkpoint、规则工具、OneAPI 客户端、预算决策和 trace 的基础实现也已存在；但输入契约（GitHub/GitLab MR/PR 链接）没有实现，且 checkpoint 复用存在会静默产生错误报告的数据正确性问题。修复下面两个高优先级问题后，再进行一次完整验收。

## 修复记录（2026-08-26）

- C1：新增 `ChangeRequestAdapter` 协议及 `GitHubAdapter`/`GitLabAdapter` 接口实现；CLI 可识别对应 URL，并在远程读取未配置时给出明确错误。Markdown 离线链路仍通过 `--diff-file` 使用。
- I1：本地 diff 的默认 source URL 现在包含绝对路径和文件 SHA-256，不同输入会创建独立 run/checkpoint。
- I2：SQLite 新增 `reservations` 表；`reserve_budget`/`settle_reservation` 使用 `BEGIN IMMEDIATE` 在事务中检查并占用预算，pipeline 在请求前预留、请求后结算，避免并发超支。
- 回归测试从 36 项增加到 38 项，覆盖本地输入隔离和原子预算预留。

## Critical

### C1：GitHub/GitLab URL 输入在 CLI 中始终失败

位置：`review_agent/cli.py:39-46`、`review_agent/adapters.py:8-23`。

CLI 只有 `LocalDiffAdapter`；当调用者提供 URL 且没有 `--diff-file` 时，`_run_review` 直接抛出“remote GitHub/GitLab adapters are not configured”。`LocalDiffAdapter` 也明确拒绝非 `local://` URL。仓库中没有 `ChangeRequestAdapter` 协议或 GitHub/GitLab 实现，因此题目规定的主要输入（一个 GitLab/GitHub MR/PR 链接）无法产生报告。

这不是远程发布评论尚未实现的问题：题目允许先落地 Markdown，但仍要求从 MR/PR 链接获取变更。至少需要实现只读的 GitHub/GitLab diff 获取适配器（认证、超时和错误处理），并通过适配器接口接入现有流水线；没有凭证时应给出明确可恢复错误或支持显式离线模式。

## Important

### I1：本地 diff 使用固定 URL，导致不同输入静默复用旧 checkpoint/report

位置：`review_agent/cli.py:40-41,67-71`；`review_agent/storage.py:157-170`。

只要传入 `--diff-file` 而不显式给 URL，CLI 就把所有输入标成 `local://diff`，然后按 URL 查找最近 run。第二个不同文件会复用第一个 run 的成功 `fetch/sanitize/tools/review/render` checkpoint，适配器不会再次读取新 diff，最终报告仍针对旧代码。复现方式：在同一 `--state-dir` 先用 `a.diff` 运行，再用 `b.diff` 运行；第二份报告中的输入和 finding 仍来自 `a.diff`。

这会在 CI 或开发者重复审查时产生静默的错误审查结果。run identity 必须包含源 revision/diff SHA（本地至少以文件内容 hash 或路径+内容 hash 作为 URL/键的一部分），恢复前还应校验当前输入 hash 与 `fetch` checkpoint 一致；输入变化时创建新 run，而不是继续使用旧 render checkpoint。远程 PR 也应使用 head SHA，而不能只用 PR URL，以避免新提交复用旧报告。

### I2：预算预留不是跨进程原子操作，竞争运行可能超出总预算

位置：`review_agent/budget.py:22-57`、`review_agent/pipeline.py:125-140`、`review_agent/storage.py:137-154`。

`reserved_usd` 和 reservation 字典只存在当前进程内；checkpoint 只记录状态，数据库没有原子“检查剩余预算并预留”操作或 run 锁。两个进程/重启竞态都可能同时读到相同 `cost_usd`，各自成功 `select/reserve` 并发起 OneAPI 请求，累计实际成本超过配置预算。`update_run_cost` 只是事后做加法，不能阻止已经发出的第二个请求。

如果首版明确限制单 run 单进程执行，应在 CLI/存储层加运行锁并在报告中声明；否则应把 reservation 和预算校验放进同一 SQLite 事务（含唯一 reservation 状态、过期恢复策略），在请求发出前完成跨进程原子扣减。

## Minor

### M1：`RunConfig.max_diff_chars` 没有生效

位置：`review_agent/models.py:86`、`review_agent/pipeline.py:127-140`。

配置字段存在，但 pipeline 只有在预算降级时使用 `Decision.max_chars`，正常预算充足时不会应用 `max_diff_chars`。调用者无法依靠该配置限制发送给模型的上下文大小；应在构造 prompt 或预算决策前统一应用硬上限，并在 trace/报告中记录截断。

### M2：trace 只保存原始 diff 的 hash，不保存可由 trace 单独复核的原始 diff

位置：`review_agent/pipeline.py:72,84,101,148`、`review_agent/models.py:52-67`。

每条 finding 都有 trace ID，且 prompt/reply 会脱敏；但 trace 本身只有 `input_hash`，没有原始 diff 字段。当前可以通过同一 run 的 `fetch` checkpoint 间接关联原始 diff，这与设计文档“保存 diff hash、原始 diff 留在本地”的方案一致，因此不是安全漏洞；不过若验收标准按“每条 trace 包含原始 diff”理解，审计 UI 仍无法只凭 trace 记录复核输入。建议在文档中明确 hash+run checkpoint 的关联语义，或增加受控的本地 diff artifact 引用（不要把 secret 放入 LLM trace）。

### M3：报告中的 URL 未做脱敏

位置：`review_agent/report.py:17`。

trace 的 prompt/reply/error 会再次脱敏，但 `result.request.url` 直接写入 Markdown。带 query token 的 URL（虽然不推荐）可能因此落盘到报告。应对 URL 做凭证参数过滤或只输出规范化的仓库/PR 标识。

## 已验证且符合要求的部分

- SQLite `runs/checkpoints/traces` 可跨实例恢复；失败阶段不会被当成成功 checkpoint。
- LLM 调用前对 diff 脱敏，工具 runner 也只收到脱敏后的 `ChangeRequest`/diff；CLI 没有执行仓库任意命令的路径。
- `ToolSpec`/`ToolRegistry` 支持声明式注册，finding 置信度被规范化为 `high` 或 `advisory`。
- OneAPI 使用 OpenAI chat-completions 兼容协议，缺失 usage 时不会按零成本计费。
- 预算决策覆盖 fallback、上下文截断和禁用 LLM；provider 回报超预算时不会把回复作为正常 finding 采纳，账面成本封顶。
- 每条工具/LLM finding 都有 `trace-*` ID，报告按“高置信度：可直接采纳”和“建议：仅供参考”分组，并附 trace appendix。
- `pytest -q` 全量 36 个测试通过；测试未覆盖远程适配器、输入 revision 变化和跨进程预算竞态。
