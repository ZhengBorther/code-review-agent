# Code Review Agent

一个以 MDR（Markdown Review Rule）为唯一依据的 Code Review Agent。输入 GitHub PR、GitLab MR 或本地 unified diff，输出带规则、证据和 trace 的 Markdown 报告。

## 特性

- 严格 `mdr_only`，不做通用自由 review。
- 按语言批量执行 MDR，支持受限并发。
- LangGraph 负责编排，SQLite 负责 checkpoint、预算和审计。
- 只读获取 GitHub/GitLab 数据，不执行仓库代码或 shell 命令。

## 快速开始

要求 Python 3.11+：

```bash
python3 -m pip install -e .
python3 -m review_agent review \
  https://github.com/org/repo/pull/123 \
  --config conf/review-agent.toml
```

PR/MR 链接是正式输入方式。`--diff-file` 只用于本地离线、断网或固定 diff 复现：

```bash
python3 -m review_agent review \
  --diff-file tests/fixtures/go-many-parameters.diff \
  --config conf/review-agent.toml \
  --rules-dir examples/rules \
  --offline
```

## 文档

- [架构](docs/architecture.md)
- [配置](docs/configuration.md)
- [MDR 规则与新增方式](docs/rules.md)
- [运行、预算、恢复与安全](docs/operations.md)
- [离线评测和验证](docs/evaluation.md)

## 开发验证

```bash
python3 -m pytest -q
git diff --check
```
