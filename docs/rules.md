# MDR 规则

## 规则是什么

MDR 是带 YAML front matter 的 Markdown 文件，只按数据解析；规则中的 Go、Python、shell 示例不会执行。

目录示例：

```text
rules/
├── go/GO-STYLE-001.mdr
├── python/PY-SEC-001.mdr
└── python/PY-STYLE-001.mdr
```

当前内置安全规则包括 `PY-SEC-001`：禁止将密码、令牌、密钥等敏感凭据传给 `print`、`logging` 或其他日志输出函数。

## 最小格式

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

校验规则：

- `id` 必须唯一且使用大写规则 ID 格式；重复 ID 会报告来源文件。
- `severity` 只能是 `error`、`warning`、`info`。
- `deprecated: true` 的规则不参与评审。
- `common` 规则适用于所有语言；未知扩展名进入 `unknown` 分组。
- 新增规则只需把 `.mdr` 放入显式授权目录，不需要修改 Pipeline。

模型必须返回 JSON。每条有效 finding 必须包含已加载的 `rule_id` 和 diff 中存在的 `file_path`；文件级问题可将 `line_start`、`line_end` 设为 `null`。MDR finding 的 `confidence` 始终是 `advisory`，severity 由 MDR 提供。

## 新增规则

1. 在已授权目录新增 `.mdr` 文件。
2. 填写 front matter 和规则正文。
3. 用 `--rules-dir` 或 `[rules].directories` 显式授权目录。
4. 运行离线测试确认 rule ID、严重度和 finding 位置。

不需要修改 `pipeline.py` 或主流程。
