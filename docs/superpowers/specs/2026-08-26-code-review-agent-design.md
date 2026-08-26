# Code Review Agent 设计

## 目标

构建一个 Python CLI：输入 GitHub/GitLab Pull Request 或 Merge Request 链接（也支持显式的本地 diff 文件），输出可审计的 Markdown Code Review 报告。首版只在本地生成报告，同时为未来远端回评保留接口。

## 架构

系统采用带 checkpoint 的五阶段流水线：`fetch`、`sanitize`、`tools`、`review`、`render`。`ChangeRequestAdapter` 提供变更请求元信息和 unified diff；安全层在任何模型调用前移除 secret；已注册工具产出结构化 finding；注入的 LLM 客户端产出建议型 finding；渲染器生成带置信度标签和 trace ID 的 Markdown。

CLI 使用稳定的 `run_id` 和 SQLite 状态库。每个阶段保存 JSON 结果；恢复运行时跳过已有成功 checkpoint，避免重复执行已完成阶段。原始 diff 仅保存在本地，并记录 SHA-256；含 secret 的内容不会发送给模型。

## 组件

- `review_agent/cli.py`：命令解析、配置读取和退出码。
- `review_agent/adapters.py`：`ChangeRequestAdapter` 协议及本地 diff 实现；新增 GitHub/GitLab 实现无需修改编排流程。
- `review_agent/pipeline.py`：阶段顺序、checkpoint 恢复、预算决策和结果汇总。
- `review_agent/tools.py`：`ToolSpec`、注册表及内置安全分析器；新增工具只需声明式注册。
- `review_agent/security.py`：secret 检测和确定性脱敏。
- `review_agent/llm.py`：`LLMClient` 协议、兼容 OneAPI 的 OpenAI 客户端及离线确定性客户端。
- `review_agent/storage.py`：SQLite schema 及原子 checkpoint/trace 操作。
- `review_agent/report.py`：确定性的 Markdown 渲染。

## 接口

```python
class ChangeRequestAdapter(Protocol):
    def fetch(self, url: str) -> ChangeRequest: ...

class LLMClient(Protocol):
    def review(self, *, prompt: str, model: str) -> LLMResponse: ...

@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    runner: Callable[[ChangeRequest, str], list[Finding]]
    confidence: Literal["high", "advisory"]
```

`Finding` 包含 `title`、`body`、可选的文件/行号位置、置信度、证据和 trace ID。`LLMResponse` 包含文本、prompt/completion token 数量及估算成本。

## 持久化与恢复

SQLite 表为 `runs`、`checkpoints` 和 `traces`。run 保存 URL、配置快照、预算、累计成本和状态；checkpoint 保存阶段、状态、JSON 输出及时间戳；trace 保存工具名、输入/diff 哈希、完整 prompt 和模型回复（均已脱敏）、模型、用量、成本、耗时和错误。checkpoint 写入使用事务；失败阶段会在下一次调用时重试。

## 预算与模型降级

`--budget-usd` 设置总预算，并提供明确的小额默认值；每次 LLM 调用前检查剩余预算。预计调用将超预算时依次执行：切换 `fallback_model`、将脱敏后的 diff 截断到配置的字符上限、最终关闭 LLM 评审但保留确定性规则结果。报告记录实际花费和降级原因。网络重试次数有上限，每次尝试都有独立 trace。

## 安全边界

Secret 检测覆盖常见 API key/token、私钥、密码赋值和高熵值。匹配内容在构造 prompt 前替换为稳定占位符。原始 diff 保存在状态目录中，LLM 请求只包含脱敏后的内容。默认不执行仓库命令；未来的命令型工具必须声明可执行文件白名单、工作目录和超时时间，未声明的命令一律拒绝。

## 报告

Markdown 报告包含 run 元数据、汇总数量、预算使用情况、降级说明，并按置信度分组展示 finding。高置信度 finding 必须引用确定性规则或测试证据，并标记为“可直接采纳”；建议型 finding 标记为“仅供参考”。每条 finding 只关联一个 trace ID，trace 区域列出工具、prompt、模型回复和输入哈希。

## 测试

单元测试和集成测试覆盖：中断后的 checkpoint 恢复、预算 fallback/截断/禁用 LLM、secret 脱敏、声明式工具注册、trace 完整性、置信度渲染、OneAPI 响应解析，以及使用 fixture diff 的离线端到端 CLI 运行。测试不会执行 fixture 仓库中的代码，也不依赖网络。

## v1 不包含

- 向 GitHub/GitLab 发布评论或 review 状态。
- 执行任意仓库测试、构建或 shell 命令。
- 多用户服务部署或分布式任务队列。
