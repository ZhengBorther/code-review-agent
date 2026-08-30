# 评测

## 离线评测

评测 case 位于：

```text
eval/cases/<RULE_ID>/<case-name>/
  patch.diff
  expect.json
```

运行：

```bash
python3 -m pytest -q tests/test_eval_cases.py
```

评测使用 fixture diff 和注入的模型响应，不访问网络，也不执行 fixture 代码。

## PR 链接验证

生产入口应直接使用 PR/MR URL；只有在网络不可用时才切换到保存的本地 diff：

```bash
python3 -m review_agent review \
  https://github.com/org/repo/pull/123 \
  --config conf/review-agent.toml
```

## 完整验证

```bash
python3 -m pytest -q
python3 -m review_agent --help
python3 -m review_agent review \
  --diff-file tests/fixtures/go-many-parameters.diff \
  --config conf/review-agent.toml \
  --rules-dir examples/rules \
  --output /tmp/mdr-review.md \
  --state-dir /tmp/mdr-review-state \
  --offline
git diff --check
```

应确认：所有测试通过；报告包含 MDR rule ID、batch trace 和 finding trace；默认模式没有通用自由 review；secret 不出现在 prompt、trace 或报告中；修改规则后对应 checkpoint 失效，其他语言 checkpoint 继续复用。
