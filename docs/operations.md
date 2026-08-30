# 运行与安全

## 输入方式

正式输入是 GitHub Pull Request 或 GitLab Merge Request 链接：

```bash
python3 -m review_agent review \
  https://github.com/org/repo/pull/123 \
  --config review-agent.toml
```

公开仓库通常不需要平台 token；私有仓库需要在 `[github].token`/`[gitlab].token` 或环境变量中提供凭据。`--diff-file` 只用于本地离线、断网或固定 diff 复现。

## 输出

配置：

```toml
[review]
output = "review.md"
state_dir = ".review-state"
```

相对路径以配置文件所在目录为基准。CLI 的 `--output` 和 `--state-dir` 优先级更高。

报告包含规则 ID、严重度、文件和行号、置信度分组、batch/finding trace、token、成本、耗时、错误和降级说明。

## Budget

`budget_usd` 是单个 PR/MR run 的预算。执行前检查：

```text
已消费成本 + 进行中 reservation + 新请求预留 <= budget_usd
```

预算不足依次尝试主模型、fallback、截断上下文，最后禁用该批次。实际成本超预算时不生成成功 finding，并写入错误 trace。

## Checkpoint 和恢复

```text
fetch
sanitize
tools
rules:<language>:<batch>
review
render
```

恢复：

```bash
python3 -m review_agent review --run-id <run-id> --config review-agent.toml
```

成功阶段跳过，失败阶段重试。provider 结果和成本在 SQLite 事务中保存；进程中断后不会盲目重复可能已计费的请求。

## 安全边界

- MDR、profile 和 diff 只作为数据解析，不执行其中的代码或命令。
- API key、私钥、AWS key、GitHub/GitLab/Slack token、Bearer token、password 等会被替换为 `[REDACTED:...]`。
- 进入 LLM、trace 和报告的内容都经过脱敏。
- GitHub/GitLab 适配器只发起只读 HTTP 请求。
- 不加载被评审仓库中未显式授权的规则目录。
- 项目不提供 ShellTool，不在用户仓库运行任意命令。
