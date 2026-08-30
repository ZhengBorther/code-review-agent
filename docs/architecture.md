# 架构

## 总体流程

```text
CLI
  -> 输入 PR/MR URL（正式）或本地 diff（离线备用）
  -> LangGraph: prepare -> review_mdr_pipeline -> deliver
  -> ReviewPipeline
       fetch -> sanitize -> tools -> MDR batches -> render
  -> Markdown 报告
```

LangGraph 负责固定编排；详细阶段的幂等性、预算和恢复由 SQLite-aware `ReviewPipeline` 管理。

## 核心组件

| 文件 | 职责 |
| --- | --- |
| `cli.py` | 参数解析、配置合并、适配器和客户端选择 |
| `adapters.py` | 本地 diff、GitHub PR、GitLab MR 只读获取 |
| `config.py` | TOML、环境变量和 CLI 配置解析 |
| `rules.py` | MDR 加载、校验、去重、筛选、规则集哈希 |
| `diff_languages.py` | unified diff 文件路径和语言分组 |
| `rule_review.py` | MDR prompt、Pydantic JSON 校验和 finding 转换 |
| `pipeline.py` | checkpoint、MDR 批次、预算 reservation、恢复 |
| `storage.py` | SQLite runs/checkpoints/traces/reservations/leases |
| `graph_pipeline.py` | LangGraph 入口 |
| `report.py` | 脱敏 Markdown 渲染 |

## 批处理

同一语言的规则合并为一个模型请求，不同语言批次按 `review.max_concurrency` 受限并发；同一语言内部按 rule ID 稳定排序。所有批次仍需通过 SQLite 原子 reservation。

## 状态恢复

SQLite 保存：

```text
runs
checkpoints
traces
reservations
run_leases
```

成功阶段跳过，失败阶段重试。provider 已完成但进程随后崩溃时，从原子保存的结果重建 finding；run lease 和 owner fencing 防止旧进程覆盖新结果。
