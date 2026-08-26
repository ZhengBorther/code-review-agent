# Code Review Agent 最终验收报告

验收版本：`bed6b47`（HEAD）

验收依据：实现计划 `docs/superpowers/plans/2026-08-26-code-review-agent.md`、设计文档 `docs/superpowers/specs/2026-08-26-code-review-agent-design.md`，以及当前源码和测试。

## 验证结果

- `pytest -q`：`40 passed in 0.18s`。
- `python3 -m review_agent review --diff-file tests/fixtures/sample.diff --output ... --state-dir ... --offline`：返回 0，生成中文 Markdown，包含高置信度/建议分组、预算和 trace 附录。
- `python3 -m review_agent --help`：返回 0。
- `git diff --check`：通过；验收期间未修改实现代码。

## Verdict

- **Spec verdict：FAIL。** 本地 diff 的 checkpoint、预算、trace、脱敏和 Markdown 产出基本符合计划；但“secret 不能上传 LLM”不是当前实现的不变量，且远程 URL 的凭证边界和 revision identity 不安全。
- **Code verdict：FAIL。** 40 项测试全绿，但测试未覆盖下列阻断场景；修复后需要重新验收。

## 阻断问题

### [Critical] 标准 AWS access key 未被脱敏，能够进入 LLM prompt

位置：`review_agent/security.py:24`。

`_API_KEY` 规则要求 `AKIA` 后必须紧跟 `-` 或 `_`，而标准 AWS access key ID 形如 `AKIAIOSFODNN7EXAMPLE`，因此不会命中。实际用 pipeline 和一个捕获 prompt 的 LLM 客户端复现：该值原样出现在 prompt 中。这个结果直接违反设计与题目要求的“不能把 secret 上传 LLM”。应覆盖标准 AWS key 及其它供应商凭证格式，并补充从 pipeline 到客户端的端到端断言。

### [Critical] GitLab host 判断可将 `GITLAB_TOKEN` 发往任意恶意域名

位置：`review_agent/cli.py:56-57`、`review_agent/adapters.py:71-79`。

CLI 使用 `"gitlab" in host` 选择 GitLabAdapter；适配器随后把 URL 中的任意 host 拼入 API 地址，并在请求中加入 `PRIVATE-TOKEN`。例如 `https://evil-gitlab.example/...` 会被识别并收到调用方的 GitLab token。应默认只允许 `gitlab.com`，自托管 GitLab 必须通过显式 allowlist/configuration 指定，且应增加 host 验证测试。

## 重要问题

### [Important] 远程 PR/MR 只按 URL 复用 checkpoint，新增提交会得到旧报告

位置：`review_agent/cli.py:81-84`、`review_agent/pipeline.py:68-74`、`review_agent/adapters.py:47-49`。

远程 URL 的 identity 是固定 PR/MR URL；CLI 查找该 URL 的最新 run 后直接复用成功的 `fetch` checkpoint，GitHub/GitLab adapter 没有把 head SHA 纳入 identity 或恢复校验。PR 推送新提交后，重复同一命令可能静默返回旧 diff 的报告。应记录并校验 provider revision/head SHA（或当前 diff hash），变化时新建 run。

### [Important] GitHub diff 请求未携带认证头，私有 PR 会在 metadata 成功后获取 diff 失败

位置：`review_agent/adapters.py:39-44`。

metadata 请求带了 `Authorization`，但对 `metadata["diff_url"]` 创建的第二个请求只设置了 `User-Agent`。`GITHUB_TOKEN` 因此没有用于 diff 下载；私有仓库通常会返回 404/401。应在 diff 请求复用受控认证头，并验证不会把 token 发往非 GitHub allowlist host。

## 非阻断缺口

- `RunConfig.max_diff_chars` 未在预算充足的正常路径中生效；目前只有预算降级时才截断 prompt（`review_agent/pipeline.py:127-140`）。
- 报告直接渲染 `result.request.url`（`review_agent/report.py:20`）；带 query credential 的输入 URL 可能落入 Markdown，建议清理凭证参数。
- trace 保存原始 diff 的 hash，并通过同一 run 的 fetch checkpoint 间接关联原文，没有受控 artifact 引用；这与当前文档的 hash 方案基本一致，但审计语义应明确。
- 预算 reservation 的请求前原子占位已实现，但 provider 实际成本高于预留、且同一 run 存在其它 in-flight reservation 时，外部实际账单仍可能超过预算；当前只能封顶本地记账并拒绝该回复。

## 已通过能力

- `ToolSpec`/`ToolRegistry` 声明式注册，工具在进程内执行，没有仓库 shell/脚本执行路径。
- SQLite runs/checkpoints/traces 可跨实例读取；失败 checkpoint 不会被当成成功阶段。
- OneAPI OpenAI-compatible 请求、缺失/零 usage 的保守估算、fallback/截断/禁用 LLM 的预算策略均有测试。
- 每个 finding 有 trace ID；报告提供“可直接采纳”和“仅供参考”置信度分组及 trace 附录。

