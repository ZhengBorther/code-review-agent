# cr-agent 模式增量优化设计

## 目标

在保持当前 MDR-only、安全脱敏、SQLite 预算和恢复语义不变的前提下，吸收 `/Users/lizheng37/baidu/bcc/cr-agent` 中有价值的工程模式：显式状态图、Pydantic 结构化模型、异步任务调度、仓库 profile 和评测用例。

## 范围

- 不引入通用自由 review；所有模型请求必须来自 MDR 规则批次。
- 不接入 `cr-agent` 的 ShellTool，不执行用户仓库代码或命令。
- 不替换现有 SQLite；SQLite 仍是 checkpoint、预算、reservation 和 trace 的事实来源。
- 将规则输出从手写 JSON 校验增强为 Pydantic schema，并保留未知字段拒绝和 advisory 强制约束。
- 用 LangGraph 声明唯一固定阶段图；LangGraph 是正式依赖，不保留第二套 fallback。
- 独立语言批次可以异步调度，但每个批次必须先通过 SQLite 原子预算 reservation。
- 增加按仓库路径选择规则目录、语言白名单和文件黑名单的 profile 配置。
- 增加离线 eval case，验证 MDR 命中、未知输出拒绝和规则变更后的 checkpoint 失效。

## 目标流程

```text
ReviewState
  fetch -> sanitize -> load_rules -> split_languages
       -> review_mdr_batches -> render
                    |
             SQLite checkpoint/trace/reservation
```

LangGraph 只负责编排状态节点；每个节点内部调用现有 adapter、RuleRegistry、BudgetController 和 StateStore。MDR 批次节点接收脱敏 diff 和已筛选规则，输出 Pydantic 校验后的 finding。

## 结构化输出

模型响应必须符合：

```json
{
  "findings": [
    {
      "rule_id": "GO-STYLE-001",
      "file_path": "main/user.go",
      "line_start": 16,
      "line_end": null,
      "title": "函数参数超过4个",
      "body": "建议使用参数结构体",
      "evidence": "函数包含7个业务参数"
    }
  ]
}
```

Pydantic 模型负责字段类型、长度、行号和数量上限；解析器再根据当前批次规则集合校验 `rule_id` 和文件路径，并强制 `confidence="advisory"`、`severity` 来自 MDR。

## 异步与恢复

异步调度只并行相互独立的语言批次。每个批次使用以下持久化协议：

```text
写 reservation checkpoint(pending)
  -> SQLite reserve_budget（BEGIN IMMEDIATE）
  -> 更新 checkpoint(in_flight)
  -> 调用 LLM
  -> SQLite settle_reservation + 保存 provider result
  -> 写 mdr_batch/mdr_finding trace
  -> 写规则 checkpoint(completed)
```

进程重启时优先读取 SQLite provider result 和 reservation 状态；如果结果已结算则重建 finding，不重复请求；如果请求状态未知则生成 recovery trace，不自动重复付费调用。

## Profile

配置文件增加可选 profile：

```toml
[[profiles]]
name = "cr-agent-code"
repo = "/Users/lizheng37/Documents/ChatGPT/code-review-agent"
rules_dirs = ["./examples/rules"]
enabled_languages = ["go"]
skip_globs = ["vendor/**", "*.generated.go"]
```

Profile 只控制规则选择和文件跳过，不改变安全边界，也不执行 profile 中的命令。

## Eval

离线 eval case 使用 fixture diff、规则 ID 和期望命中结果：

```text
eval/cases/GO-STYLE-001/many-parameters/
  patch.diff
  expect.json
```

eval 不访问网络、不执行 fixture 仓库代码，验证批次调用次数、命中 rule_id、advisory 置信度和拒绝未知输出。

## 失败策略

- Pydantic/JSON 解析失败：记录 batch trace，当前批次不产出 finding。
- 预算不足：按现有 fallback、截断、禁用顺序处理；不会超预算崩溃。
- LangGraph 不可用：启动失败并提示安装项目依赖，避免执行路径分叉。
- Profile 或规则配置错误：在启动阶段报告具体文件和字段并退出。
