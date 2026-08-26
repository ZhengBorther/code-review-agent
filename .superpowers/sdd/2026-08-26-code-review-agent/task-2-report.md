# Task 2 完成报告

## 改动

- 新增 `review_agent/storage.py`，使用标准库 SQLite 持久化 `runs`、`checkpoints`、`traces` 三张表。
- checkpoint 以 `(run_id, stage)` 为联合主键，支持事务性 upsert，进程重启后可恢复阶段输出。
- trace 以 `trace_id` 为主键幂等保存完整 `TraceRecord` JSON，并记录关联 run 的更新时间。
- run 保存 URL、配置快照、预算、累计成本、状态和 UTC 时间戳；提供成本增量和状态/成本更新辅助。
- 新增 `tests/test_storage.py`，覆盖跨实例恢复、run 元数据、checkpoint upsert、trace 审计字段与成本更新、状态更新。

## 测试

- `pytest -q tests/test_storage.py`: 5 passed
- `pytest -q`: 8 passed
- `git diff --check`: passed

## 风险

- 数据库 schema 当前由启动时 `CREATE TABLE IF NOT EXISTS` 管理，尚未提供版本化迁移机制。
- trace 的 prompt/response 是否脱敏由上游安全层保证，存储层按调用方传入内容原样落盘。
