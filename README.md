# Code Review Agent

一个可恢复、可审计的 Code Review CLI。支持显式 unified diff，也支持只读获取 GitHub Pull Request / GitLab Merge Request diff，最终生成本地 Markdown 报告。

## 离线运行

离线模式不会发起网络请求，使用确定性模型，适合 CI 和本地验证：

```bash
python3 -m review_agent review \
  --diff-file tests/fixtures/sample.diff \
  --output report.md \
  --state-dir .review-state \
  --offline
```

状态目录中的 `state.db` 保存 run、阶段 checkpoint 和 trace。重复执行同一 URL/run ID 会复用成功阶段，不会从头调用模型。

## OneAPI

OneAPI 使用 OpenAI Chat Completions 兼容协议。模型、fallback、超时、费率和 API key 都可写在统一 TOML 配置中；生产环境更推荐通过环境变量注入：

```bash
# Credentials may come from this config file or environment variables.
export ONEAPI_API_KEY=your-key
python3 -m review_agent review \
  --diff-file change.diff \
  --config review-agent.example.toml
```

配置文件可同时管理 `[review]`、`[llm]`、`[llm.pricing]` 和 `[rules]`。命令行参数覆盖 TOML，环境变量覆盖 TOML；预算不足时会依次尝试降级模型、截断上下文，最后只保留规则工具结果。

`[review].mode` 默认为 `mdr_only`，只按 MDR 规则生成评论。只有显式设置 `mode = "hybrid"` 或 `mode = "generic"` 时，才会启用通用 LLM review；通用 review 产生的评论不带 MDR `rule_id`，仅供参考。

`[llm.pricing]` 的单位是 **USD / 1,000 tokens**。例如 `qwen-plus = 0.003` 表示每 1,000 tokens 估算成本为 `$0.003`。当前版本使用 prompt tokens 和 completion tokens 的合计数量乘以该单一费率；如果模型供应商区分输入和输出价格，需要先换算成一个 blended rate。

配置优先级为：代码默认值 < TOML 文件 < 环境变量 < CLI 参数。OneAPI、GitHub、GitLab token 都支持放入 TOML，但不写入 SQLite 运行快照、checkpoint 或 trace。包含 token 的配置文件必须限制为当前用户可读：

```bash
chmod 600 review-agent.toml
```

也可以继续使用 `ONEAPI_API_KEY`、`GITHUB_TOKEN` 和 `GITLAB_TOKEN` 环境变量；环境变量优先于 TOML，CLI 的 `--oneapi-api-key`、`--github-token` 和 `--gitlab-token` 优先级最高。

## GitHub / GitLab

直接传入 PR/MR URL 即可读取变更元数据和 diff；私有项目分别使用 `GITHUB_TOKEN`、`GITLAB_TOKEN`。适配器只发起只读 HTTP 请求，不克隆仓库，也不执行仓库代码。首版仍只生成本地报告，不会向远端发布评论。

## 安全边界

- 发送给 LLM 前会对常见 API key、token、密码和私钥进行确定性脱敏；原始 diff 只写入本地状态目录。
- CLI 不执行仓库中的脚本或任意命令；`--diff-file` 必须由调用方显式指定。
- 报告按“高置信度：可直接采纳”和“建议：仅供参考”分级，并为每条 finding 附 trace ID。trace 记录工具、输入哈希、脱敏 prompt、模型回复和成本。

## MDR 规则

MDR（Markdown Review Rule）是只包含 YAML front matter 和说明文本的规则文件。它不是 Python 插件，规则中的代码示例只会作为评审上下文，永远不会被执行。

规则目录示例：

```text
examples/rules/
└── go/GO-STYLE-001.mdr
```

一个最小规则需要这些 front matter 字段：`id`、`title`、`language`、`domains`、`severity`、`prompt_hint` 和 `deprecated`。`severity` 可取 `error`、`warning` 或 `info`；LLM 产生的 MDR finding 始终是“仅供参考”，只有规则工具或测试证据支持的 finding 才能是高置信度。

可以复制示例配置后显式授权规则目录：

```bash
python3 -m review_agent review \
  --diff-file tests/fixtures/go-many-parameters.diff \
  --config review-agent.example.toml \
  --rules-dir examples/rules \
  --output /tmp/mdr-review.md \
  --state-dir /tmp/mdr-review-state \
  --offline
```

配置文件中的 `[rules].directories` 相对路径以配置文件所在目录为基准；重复的 CLI `--rules-dir` 会追加并去重。用户默认目录为 `~/.config/code-review-agent/rules.d`。CLI 目录优先追加到配置目录，规则按 ID 排序，重复 ID 会使运行失败。`enabled_languages` 限制语言，`disabled_rules` 禁用指定 ID，`deprecated: true` 的规则会跳过。

变更按文件扩展名分组：Go、Python、TypeScript、Java 等各自产生语言批次；`common` 规则会加入每个批次。同一语言的规则默认合并为一次模型请求，超出上下文限制时才拆分。每个批次有 `mdr_batch` trace，每条 finding 另有带父 trace 的 `mdr_finding` trace。

规则集哈希和 diff 哈希会写入 checkpoint。修改规则或相关语言的 diff 后，只会重新执行受影响的语言批次；断网或重启可以继续使用已有 checkpoint。非法 YAML、缺少字段、非法 ID/severity 或重复 ID 会报告具体文件并终止本次运行。

运行测试：`pytest -q`。
