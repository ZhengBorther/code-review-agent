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

OneAPI 使用 OpenAI Chat Completions 兼容协议。可通过参数或环境变量配置：

```bash
export ONEAPI_BASE_URL=https://oneapi.example/v1
export ONEAPI_API_KEY=your-key
python3 -m review_agent review --diff-file change.diff --output report.md
```

可选参数包括 `--model`、`--fallback-model` 和 `--budget-usd`。预算不足时会依次尝试降级模型、截断上下文，最后只保留规则工具结果。

## GitHub / GitLab

直接传入 PR/MR URL 即可读取变更元数据和 diff；私有项目分别使用 `GITHUB_TOKEN`、`GITLAB_TOKEN`。适配器只发起只读 HTTP 请求，不克隆仓库，也不执行仓库代码。首版仍只生成本地报告，不会向远端发布评论。

## 安全边界

- 发送给 LLM 前会对常见 API key、token、密码和私钥进行确定性脱敏；原始 diff 只写入本地状态目录。
- CLI 不执行仓库中的脚本或任意命令；`--diff-file` 必须由调用方显式指定。
- 报告按“高置信度：可直接采纳”和“建议：仅供参考”分级，并为每条 finding 附 trace ID。trace 记录工具、输入哈希、脱敏 prompt、模型回复和成本。

运行测试：`pytest -q`。
