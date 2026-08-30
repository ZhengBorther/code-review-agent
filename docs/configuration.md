# 配置

## 统一 TOML

```toml
[review]
mode = "mdr_only"
budget_usd = 10.0
max_diff_chars = 12000
# 结构化 MDR 输出默认使用 Codex 配置中的非思考模型，避免空回复。
completion_tokens = 1024
max_concurrency = 2
output = "../review.md"
state_dir = "../.review-state"

[llm]
base_url = "https://oneapi-comate.baidu-int.com/v1"
model = "gpt-5.6-sol"
fallback_model = "gpt-5.6-sol"
timeout_seconds = 120
# 在本机填写，不要提交真实 token。
# api_key = "replace-with-your-oneapi-token"

[llm.pricing]
# 单位：USD / 1,000 tokens，prompt 和 completion 合计。
qwen-plus = 0.003
qwen-turbo = 0.001

[github]
# 私有 GitHub 仓库需要填写；公开仓库可留空。
# token = "replace-with-your-github-token"

[gitlab]
# 私有 GitLab 仓库需要填写；公开仓库可留空。
# token = "replace-with-your-gitlab-token"

[rules]
directories = ["../rules"]
enabled_languages = []
disabled_rules = []
```

默认配置：[conf/review-agent.toml](../conf/review-agent.toml)。不传 `--config` 时自动读取该文件；其中相对路径相对该文件目录解析。

正式运行传入 PR/MR URL：

```bash
python3 -m review_agent review \
  https://github.com/org/repo/pull/123 \
  --config conf/review-agent.toml
```

支持的 URL 格式：

```text
https://github.com/{owner}/{repo}/pull/{number}
https://gitlab.com/{group}/{project}/-/merge_requests/{number}
```

`--diff-file` 仅用于本地离线或断网测试，不是正式输入要求。

配置优先级：

```text
代码默认值 < TOML < 环境变量 < CLI 参数
```

CLI 覆盖示例：

```bash
python3 -m review_agent review \
  --diff-file change.diff \
  --config conf/review-agent.toml \
  --model gpt-5.6-sol \
  --budget-usd 2.0
```

## Token

OneAPI token 直接配置在默认文件的 `[llm]` 表中：

```toml
[llm]
api_key = "replace-with-your-oneapi-token"
```

私有仓库的访问凭据分别配置在 `[github].token` 或 `[gitlab].token`。环境变量 `ONEAPI_API_KEY`、`OPENAI_API_KEY`、`GITHUB_TOKEN`、`GITLAB_TOKEN` 仍可作为临时覆盖，命令行参数优先级最高。优先级为：代码默认值 < TOML < 环境变量 < CLI 参数。

token 不写入 SQLite 运行快照、checkpoint 或 trace。包含真实 token 的配置文件应设置：

```bash
chmod 600 conf/review-agent.toml
```

不要把真实 token 写入 README、测试 fixture 或提交历史；仓库中的配置只保留 `replace-with-your-...` 占位符。

## Profile

```toml
[[profiles]]
name = "my-repo"
repo = "/path/to/repository"
rules_dirs = ["./company-rules"]
enabled_languages = ["go"]
skip_globs = ["vendor/**", "*.generated.go"]
```

运行时指定：

```bash
python3 -m review_agent review \
  --diff-file change.diff \
  --profile review-profiles.toml \
  --repo-path /path/to/repository
```

Profile 只影响规则目录、语言和跳过文件，不包含命令。
