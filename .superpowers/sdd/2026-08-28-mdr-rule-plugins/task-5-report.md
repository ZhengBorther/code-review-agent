# Task 5 Report

## 完成内容

- CLI 新增 `--config PATH` 和可重复的 `--rules-dir PATH` 参数。
- TOML 中的规则目录相对配置文件所在目录解析，CLI 目录相对当前工作目录解析。
- 启动时优先加入 `~/.config/code-review-agent/rules.d`，仅当该目录存在时加载。
- 所有目录中的 `.mdr` 文件通过安全 loader 解析并注册到一个 `RuleRegistry`，随后传入 `ReviewPipeline`。
- 保留无规则调用兼容性：空 registry 不触发 MDR 批次，原有普通 review 流程不变。
- 配置、规则目录或 MDR 文件错误沿用 CLI 错误出口，报告文件路径和具体原因后返回 1。

## 验证

```text
pytest -q tests/test_cli_rules.py tests/test_cli_e2e.py
8 passed

pytest -q
79 passed

git diff --check
passed
```

新增测试覆盖 TOML 与重复 CLI 目录合并、默认用户目录发现、非法 MDR 错误信息和无规则/恢复兼容性。
