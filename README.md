# Code Review Agent

一个以 MDR（Markdown Review Rule）为唯一依据的 Code Review Agent。输入 GitHub Pull Request、GitLab Merge Request 或本地 unified diff，输出带规则、证据和 trace 的 Markdown 报告。

## 1. 架构

```text
CLI
  -> LangGraph 编排图
       prepare
       review_mdr_pipeline
       deliver
  -> SQLite-aware ReviewPipeline
       fetch -> sanitize -> tools -> MDR batches -> render
  -> Markdown 报告
```

核心组件：

| 组件 | 作用 |
| --- | --- |
| `adapters.py` | 读取本地 diff、GitHub PR、GitLab MR |
| `rules.py` | 安全加载 MDR、去重、停用、规则集哈希 |
| `diff_languages.py` | 从 unified diff 识别并分组语言 |
| `rule_review.py` | 构造同语言 MDR 批次、校验模型 JSON |
| `pipeline.py` | checkpoint、预算、reservation、并发和恢复 |
| `storage.py` | SQLite runs/checkpoints/traces/reservations/leases |
| `graph_pipeline.py` | LangGraph 固定编排入口 |
| `report.py` | 脱敏 Markdown 渲染 |

LangGraph 是正式依赖，SQLite 是恢复、预算和审计的事实来源。项目不引入 ShellTool，也不执行被评审仓库中的代码、测试、构建或命令。

## 2. 安装

要求 Python 3.11+：

```bash
python3 -m pip install -e .
```

依赖包括 `PyYAML`、`pydantic` 和 `langgraph`。安装后检查：

```bash
python3 -m review_agent --help
```

## 3. 配置

复制 [review-agent.example.toml](review-agent.example.toml) 为本地配置：

```toml
[review]
mode = "mdr_only"
budget_usd = 10.0
max_diff_chars = 12000
completion_tokens = 512
max_concurrency = 2
output = "review.md"
state_dir = ".review-state"

[llm]
base_url = "https://oneapi.example/v1"
model = "qwen-plus"
fallback_model = "qwen-turbo"
timeout_seconds = 120
# api_key = "your-oneapi-token"

[llm.pricing]
# 单位：USD / 1,000 tokens；prompt 和 completion 使用合计 blended rate。
qwen-plus = 0.003
qwen-turbo = 0.001

[github]
# token = "your-github-token"

[gitlab]
# token = "your-gitlab-token"

[rules]
directories = ["./examples/rules"]
enabled_languages = ["go", "python"]
disabled_rules = []
```

配置优先级：

```text
代码默认值 < TOML < 环境变量 < CLI 参数
```

token 可以放在本地 TOML，也可以使用 `ONEAPI_API_KEY`、`GITHUB_TOKEN`、`GITLAB_TOKEN`。token 不写入 SQLite 运行快照、checkpoint 或 trace；配置文件应设置：

```bash
chmod 600 review-agent.toml
```

`budget_usd` 是单个 PR/MR run 的预算，不是所有任务共享的全局预算。`[llm.pricing]` 的单位是 USD / 1,000 tokens，prompt 和 completion token 合计计费。

## 4. 使用

### 本地 diff

```bash
python3 -m review_agent review \
  --diff-file change.diff \
  --config review-agent.toml
```

### GitHub PR / GitLab MR

```bash
python3 -m review_agent review \
  https://github.com/org/repo/pull/123 \
  --config review-agent.toml
```

适配器只读取元数据和 diff，不克隆仓库、不执行仓库代码。首版只生成本地 Markdown，不会向远端发布评论。

### 离线运行

```bash
python3 -m review_agent review \
  --diff-file tests/fixtures/go-many-parameters.diff \
  --config review-agent.example.toml \
  --rules-dir examples/rules \
  --offline
```

离线模式使用确定性模型，不访问 OneAPI 或 GitHub。

### 恢复任务

```bash
python3 -m review_agent review \
  --run-id <run-id> \
  --config review-agent.toml
```

本地 diff 的同一路径和内容哈希会自动复用已有 run；远程 PR 建议保存 `run_id` 并显式恢复。

## 5. MDR 规则

MDR 是带 YAML front matter 的 Markdown 文件，只按数据解析；规则中的 Go、Python、shell 示例不会执行。

目录示例：

```text
rules/
├── go/
│   └── GO-STYLE-001.mdr
└── python/
    └── PY-STYLE-001.mdr
```

最小规则示例：

```md
---
id: GO-STYLE-001
title: 函数业务参数超过4个时必须使用结构体
language: go
domains: [STYLE]
severity: warning
prompt_hint: >
  除 context.Context 外，业务参数超过4个时必须使用 Params、Options 或 Request 结构体封装。
deprecated: false
---

# GO-STYLE-001 函数业务参数超过4个时必须使用结构体

规则说明、正例和反例写在这里。
```

必填字段：`id`、`title`、`language`、`domains`、`severity`、`prompt_hint`、`deprecated`。

规则校验：

- `id` 必须唯一并使用大写规则 ID 格式；重复 ID 会报告来源文件。
- `severity` 只能是 `error`、`warning`、`info`。
- `deprecated: true` 的规则不参与评审。
- `common` 规则适用于所有语言；未知扩展名进入 `unknown` 分组。
- 新增规则只需把 `.mdr` 放入显式授权目录，不需要修改 Pipeline。

模型必须返回 JSON。每条有效 finding 必须包含已加载的 `rule_id` 和 diff 中存在的 `file_path`；文件级问题可将 `line_start`、`line_end` 设为 `null`。MDR finding 的 `confidence` 始终是 `advisory`，severity 由 MDR 提供。

## 6. 批处理、预算和并发

同一语言的规则合并为一次模型请求：

```text
Go diff + Go MDR + Common MDR -> 一个 Go batch
Python diff + Python MDR + Common MDR -> 一个 Python batch
```

不同语言 batch 按 `[review].max_concurrency` 受限并发执行；SQLite `BEGIN IMMEDIATE` 保证单个 PR/MR 的 reservation 不超预算。预算不足时依次尝试主模型、fallback、截断上下文，最后禁用该 batch；不会以超预算结果生成成功 finding。

## 7. Checkpoint 和 Trace

SQLite 保存：

```text
runs
checkpoints
traces
reservations
run_leases
```

主要 checkpoint：

```text
fetch
sanitize
tools
rules:<language>:<batch>
review
render
```

每条 MDR finding 有一个 `mdr_finding` trace，并通过 `parent_trace_id` 关联 `mdr_batch` trace。batch trace 保存规则 ID、规则集哈希、diff 哈希、脱敏 prompt、模型回复、token、成本、耗时和拒绝原因。

恢复时成功阶段跳过；失败阶段重试；已结算但后续落盘失败的模型结果从 SQLite 重建，避免重复请求。run lease 和 owner fencing 防止两个进程同时修改同一个 run。

## 8. 安全边界

- MDR、profile 和 diff 只作为数据解析，不执行其中的代码或命令。
- 常见 API key、私钥、AWS key、GitHub/GitLab/Slack token、Bearer token、password 等会被替换为 `[REDACTED:...]`。
- 原始 diff 只保存在本地状态目录；进入模型和 trace 的内容已脱敏。
- GitHub/GitLab 适配器只发起只读 HTTP 请求。
- 不加载被评审仓库中未显式授权的规则目录。
- 项目不提供 ShellTool，不在用户仓库运行任意命令。

## 9. Profile 和离线评测

Profile 可以按仓库路径选择规则和跳过文件：

```toml
[[profiles]]
name = "my-repo"
repo = "/path/to/repository"
rules_dirs = ["./company-rules"]
enabled_languages = ["go"]
skip_globs = ["vendor/**", "*.generated.go"]
```

运行：

```bash
python3 -m review_agent review \
  --diff-file change.diff \
  --profile review-profiles.toml \
  --repo-path /path/to/repository
```

离线评测 case 位于 `eval/cases/`，执行：

```bash
python3 -m pytest -q tests/test_eval_cases.py
```

## 10. 开发验证

```bash
python3 -m pytest -q
git diff --check
```

当前测试覆盖 MDR schema、语言分组、批量调用、并发预算、checkpoint 恢复、trace 关联、配置优先级、profile 和 CLI 端到端流程。
