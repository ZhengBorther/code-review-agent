# MDR 规则插件设计

## 目标

为 Code Review Agent 增加数据化的 MDR 规则插件机制。用户通过配置文件和规则目录加载 `.mdr` 文件；新增、修改或停用规则时不需要修改 CLI、工具注册表或评审流水线代码。

MDR 是带 YAML front matter 的 Markdown 规则文档，不是可执行代码。规则中的代码块仅作为正反例进入模型上下文，Agent 不执行规则目录或被评审仓库中的任何代码。

## 目录与配置

推荐目录结构：

```text
review-rules/
├── go/
│   ├── GO-STYLE-001.mdr
│   └── GO-ERROR-001.mdr
├── python/
│   └── PY-STYLE-001.mdr
└── common/
    └── COMMON-SECURITY-001.mdr
```

可选的 TOML 配置文件：

```toml
[rules]
directories = [
  "~/.config/code-review-agent/rules.d",
  "./company-review-rules"
]
enabled_languages = ["go", "python"]
disabled_rules = ["GO-STYLE-099"]
```

CLI 支持：

```bash
python3 -m review_agent review PR_URL \
  --config review-agent.toml \
  --rules-dir ./extra-rules
```

规则目录按以下顺序合并：默认用户规则目录、TOML `directories`、重复的 CLI `--rules-dir`。加载后按 rule ID 去重和排序。Agent 不自动加载被评审仓库中的规则；项目内目录必须由操作者通过配置或 CLI 显式授权。

## MDR 格式

MDR 使用 UTF-8 编码，由 YAML front matter 和 Markdown 正文组成：

````markdown
---
id: GO-STYLE-001
title: 函数参数超过4个时必须使用结构体封装传参
language: go
domains:
  - STYLE
severity: warning
prompt_hint: |
  检查新增或修改的函数与方法签名。
  当业务参数数量大于4个时，必须使用结构体封装。
  context.Context 可以保留为首参，不计入业务参数数量。
deprecated: false
---

# GO-STYLE-001 函数参数超过4个时必须使用结构体封装传参

## 规则说明

- 当业务参数数量大于 4 个时，必须使用结构体封装。

## 正例

```go
type CreateUserParams struct {
    Name string
}
```

## 反例

```go
func CreateUser(ctx context.Context, name string, age int, role string, active bool, region string) error {
    return nil
}
```
````

必填字段为 `id`、`title`、`language`、`domains`、`severity`、`prompt_hint` 和 `deprecated`。

校验规则：

- `id` 必须唯一，并匹配 `^[A-Z][A-Z0-9_-]+$`。
- `language` 规范化为小写；`common` 表示适用于所有语言。
- `domains` 必须是非空字符串列表。
- `severity` 只能是 `error`、`warning` 或 `info`。
- `prompt_hint` 必须是非空字符串。
- `deprecated` 必须是布尔值；值为 `true` 时跳过规则。
- 单个 MDR 文件有明确大小上限，避免异常配置消耗过多内存和 token。

## 组件

- `ReviewRule`：不可变规则数据模型，保存 front matter、Markdown 正文和安全的相对来源标识。
- `MdrRuleLoader`：扫描授权目录，使用 YAML `safe_load` 解析 front matter，执行 schema 校验。
- `RuleRegistry`：处理规则 ID 去重、停用列表、语言筛选、稳定排序和规则集哈希。
- `LanguageDetector`：根据 unified diff 文件头和文件扩展名拆分语言相关 diff。
- `RuleBatchReviewer`：为每种语言构造规则批次、调用 LLM、校验结构化输出并生成 finding/trace。

这些组件在 Agent 中一次性接入。此后增加规则只需新增 MDR 文件，不改变 `ReviewPipeline` 的阶段结构或逐规则分支。

## 语言识别与批处理

Agent 从 unified diff 的 `+++ b/path` 等文件头提取变更文件，并按扩展名识别语言。例如 `.go` 映射为 `go`，`.py` 映射为 `python`，`.ts`/`.tsx` 映射为 `typescript`，`.java` 映射为 `java`。

执行时按语言形成批次：

```text
Go diff + Go 规则 + Common 规则
Python diff + Python 规则 + Common 规则
```

同一语言的适用规则默认合并为一次 LLM 请求。若规则文本和 diff 超出上下文或预算限制，则在该语言内部按照稳定的 rule ID 顺序拆分为多个批次，且不混合其他语言。

已有模型预算策略仍然生效：主模型、fallback 模型、上下文截断、最终禁用 LLM。规则文本和 diff 均计入 token 估算。

## 模型输出

模型必须返回结构化 JSON：

```json
{
  "findings": [
    {
      "rule_id": "GO-STYLE-001",
      "file_path": "internal/user/service.go",
      "line_start": 42,
      "title": "函数业务参数超过4个",
      "body": "建议将业务参数封装为 CreateUserParams。",
      "evidence": "CreateUser 包含5个业务参数"
    }
  ]
}
```

解析器执行以下验证：

- `rule_id` 必须属于当前批次。
- `file_path` 必须属于当前批次 diff。
- `line_start` 必须为空或为正整数。
- 未知规则、未知文件和格式错误的 finding 被拒绝，并将拒绝原因写入 batch trace。
- `severity` 从 MDR 取得，不接受模型提供的严重度。

## 严重度与置信度

`severity` 表示问题一旦成立后的影响，`confidence` 表示 Agent 判断问题确实存在的可靠程度，两者互不替代。

所有仅通过 LLM 判断的 MDR finding 固定为 `advisory`，在报告中显示为“仅供参考”。MDR 不能把纯 LLM finding 声明为 `high`。只有经过 AST、正则、编译器、测试或其他确定性验证器二次验证的 finding 才能标记为“高置信度可直接采纳”。

## Trace 与 Checkpoint

每个语言批次生成 batch trace，包含：

- 规则 ID 列表及规范化 `ruleset_hash`。
- 脱敏后的相关语言 diff。
- 完整 prompt 和原始模型响应。
- 模型、token、成本、耗时及错误。
- 被拒绝的结构化 finding 及拒绝原因。

每条接受的评论生成独立 finding trace，包含 `rule_id`、`batch_trace_id`、解析后的 finding、diff 哈希、适用文件及 MDR 来源标识。每条报告评论只关联一个 finding trace，并能通过其 `batch_trace_id` 回溯完整模型请求。

规则评审 checkpoint 保存 `ruleset_hash` 和相关语言 diff 哈希。当 MDR 内容、启停列表、规则目录配置或相关语言 diff 发生变化时，只使对应规则评审 checkpoint 失效；`fetch`、`sanitize` 等无关阶段仍复用。

## 安全

- MDR 只按文本和 YAML 数据解析，代码块、模板表达式、shell 命令和 Python import 均不执行。
- YAML 使用 `safe_load`，拒绝自定义对象构造标签。
- 规则目录必须由配置或 CLI 显式授权。
- MDR 和 diff 在发送给 LLM 前都经过 secret 脱敏。
- 报告和 trace 使用规则 ID 与相对来源标识，不暴露敏感绝对路径。
- 被评审仓库不能通过提交 MDR 文件获得 Agent 执行权限。

## 错误处理

无法解析的 YAML、缺少字段、重复 ID、非法值和超出大小上限属于配置错误。默认终止运行，并报告文件和可定位的错误信息，避免悄悄漏掉组织规则。

`deprecated: true`、`disabled_rules` 或未启用语言不属于错误；规则被跳过，并在运行摘要中记录原因。

模型响应格式错误不影响其他 checkpoint。当前批次记录失败 trace；根据既有重试和预算策略决定重试或降级。

## 示例与文档

仓库提供：

- `examples/rules/go/GO-STYLE-001.mdr`
- `review-agent.example.toml`
- README 中的规则编写、校验、启用、停用和 CLI 示例。

## 测试

测试覆盖：

- 正常 MDR 解析和所有 schema 错误。
- 重复 ID、文件大小限制及安全 YAML 行为。
- `deprecated`、`disabled_rules` 和语言过滤。
- diff 文件语言识别和 Common 规则合并。
- 同语言多规则只产生一次模型调用。
- 超限语言批次的稳定拆分。
- 规则、配置或相关 diff 变化使相应 checkpoint 失效。
- 未知 rule ID、未知文件和错误 JSON finding 被拒绝并进入 trace。
- MDR finding 始终保持 `advisory`。
- MDR 代码块不会被执行。
- 规则文本中的 secret 在模型调用前被脱敏。
- 离线 CLI 加载示例 MDR 并生成带 rule ID 和 trace 的 Markdown 报告。

## 非目标

- 允许 MDR 执行 Python、shell 或仓库代码。
- 在首版中为任意自然语言规则自动生成 AST 验证器。
- 自动信任和加载被评审仓库提交的规则目录。
- 将 MDR finding 自动提升为高置信度。
