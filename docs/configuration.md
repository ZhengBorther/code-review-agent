# 配置

## 统一 TOML

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
# 单位：USD / 1,000 tokens，prompt 和 completion 合计。
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

完整样例：[review-agent.example.toml](../review-agent.example.toml)。

配置优先级：

```text
代码默认值 < TOML < 环境变量 < CLI 参数
```

CLI 覆盖示例：

```bash
python3 -m review_agent review \
  --diff-file change.diff \
  --config review-agent.toml \
  --model gpt-5.6-sol \
  --budget-usd 2.0
```

## Token

OneAPI、GitHub、GitLab token 都可以放入本地 TOML，也可以使用 `ONEAPI_API_KEY`、`GITHUB_TOKEN`、`GITLAB_TOKEN`。token 不写入 SQLite 运行快照、checkpoint 或 trace。包含 token 的配置文件应设置：

```bash
chmod 600 review-agent.toml
```

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
