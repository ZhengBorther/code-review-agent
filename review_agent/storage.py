"""用于恢复 Review run 的持久化 SQLite 状态。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import RunConfig, TraceRecord


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    """在本地 SQLite 文件中保存 run、阶段结果和审计 trace。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    budget_usd REAL NOT NULL,
                    cost_usd REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'running',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    run_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'success',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, stage),
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS traces (
                    trace_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    trace_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS reservations (
                    token TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    reserved_usd REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'in_flight',
                    actual_usd REAL NOT NULL DEFAULT 0,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS run_leases (
                    run_id TEXT PRIMARY KEY,
                    owner_token TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(checkpoints)")}
            if "status" not in columns:
                connection.execute(
                    "ALTER TABLE checkpoints ADD COLUMN status TEXT NOT NULL DEFAULT 'success'"
                )
            reservation_columns = {row["name"] for row in connection.execute("PRAGMA table_info(reservations)")}
            if "result_json" not in reservation_columns:
                connection.execute("ALTER TABLE reservations ADD COLUMN result_json TEXT NOT NULL DEFAULT '{}'")

    def create_run(self, config: RunConfig) -> str:
        run_id = str(uuid.uuid4())
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO runs
                   (run_id, url, config_json, budget_usd, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (run_id, config.url, json.dumps(config.to_dict()), config.budget_usd, now, now),
            )
        return run_id

    def acquire_run_lease(self, run_id: str, owner_token: str, ttl_seconds: int = 900) -> bool:
        """跨进程独占一个 run；过期 lease 可由后续恢复者接管。"""
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(seconds=ttl_seconds)).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner_token, expires_at FROM run_leases WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is not None and datetime.fromisoformat(row["expires_at"]) > now and row["owner_token"] != owner_token:
                return False
            connection.execute(
                "INSERT INTO run_leases (run_id, owner_token, expires_at, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET owner_token=excluded.owner_token, "
                "expires_at=excluded.expires_at, updated_at=excluded.updated_at",
                (run_id, owner_token, expires, now.isoformat()),
            )
            return True

    def release_run_lease(self, run_id: str, owner_token: str) -> None:
        """仅 lease 所有者可以释放，防止旧进程删除新 lease。"""
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM run_leases WHERE run_id = ? AND owner_token = ?",
                (run_id, owner_token),
            )

    def refresh_run_lease(self, run_id: str, owner_token: str, ttl_seconds: int) -> bool:
        """续期仍由当前 owner 持有的 lease；所有权变化时拒绝续期。"""
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(seconds=ttl_seconds)).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE run_leases SET expires_at = ?, updated_at = ? "
                "WHERE run_id = ? AND owner_token = ?",
                (expires, now.isoformat(), run_id, owner_token),
            )
            return cursor.rowcount > 0

    def get_checkpoint(self, run_id: str, stage: str) -> dict[str, Any] | None:
        """只返回成功阶段的数据；失败或过期记录仍可被审计查询。"""
        record = self.get_checkpoint_record(run_id, stage)
        if record is None or record["status"] != "success":
            return None
        return record["payload"]

    def get_checkpoint_record(self, run_id: str, stage: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json, status FROM checkpoints WHERE run_id = ? AND stage = ?",
                (run_id, stage),
            ).fetchone()
        if row is None:
            return None
        return {"payload": json.loads(row["payload_json"]), "status": row["status"]}

    def list_checkpoints(self, run_id: str, prefix: str = "") -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT stage, payload_json, status FROM checkpoints WHERE run_id = ? AND stage LIKE ? ORDER BY stage",
                (run_id, f"{prefix}%"),
            ).fetchall()
        return [{"stage": row["stage"], "payload": json.loads(row["payload_json"]), "status": row["status"]} for row in rows]

    def list_inflight_reservations(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT token, reserved_usd, created_at, updated_at FROM reservations WHERE run_id = ? AND status = 'in_flight'",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_reservation(self, run_id: str, token: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT token, reserved_usd, status, actual_usd, result_json, created_at, updated_at FROM reservations WHERE run_id = ? AND token = ?",
                (run_id, token),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["result"] = json.loads(result.pop("result_json") or "{}")
        return result

    def save_checkpoint(
        self, run_id: str, stage: str, payload: dict[str, Any], *, status: str = "success"
    ) -> None:
        """原子 upsert JSON 阶段结果，使重试保持幂等。"""
        now = _utc_now()
        payload_json = json.dumps(payload, ensure_ascii=True)
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO checkpoints
                   (run_id, stage, payload_json, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(run_id, stage) DO UPDATE SET
                     payload_json = excluded.payload_json, status = excluded.status,
                     updated_at = excluded.updated_at""",
                (run_id, stage, payload_json, status, now, now),
            )

    def mark_checkpoint(self, run_id: str, stage: str, status: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE checkpoints SET status = ?, updated_at = ? WHERE run_id = ? AND stage = ?",
                (status, _utc_now(), run_id, stage),
            )

    def supersede_checkpoint(self, run_id: str, stage: str) -> None:
        """原子地标记过期 checkpoint，并释放它关联的预算预留。"""
        now = _utc_now()
        with self._connect() as connection:
            # BEGIN IMMEDIATE 串行化过期和结算，避免旧 checkpoint 释放新请求的预算。
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload_json FROM checkpoints WHERE run_id = ? AND stage = ?",
                (run_id, stage),
            ).fetchone()
            if row is None:
                return
            payload = json.loads(row["payload_json"])
            connection.execute(
                "UPDATE checkpoints SET status = 'superseded', updated_at = ? WHERE run_id = ? AND stage = ?",
                (now, run_id, stage),
            )
            token = payload.get("token") if stage.endswith(":reservation") else None
            if token:
                connection.execute(
                    "UPDATE reservations SET status = 'released', actual_usd = 0, updated_at = ? "
                    "WHERE run_id = ? AND token = ? AND status = 'in_flight'",
                    (now, run_id, token),
                )

    def save_trace(self, trace: TraceRecord) -> None:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO traces (trace_id, run_id, kind, trace_json, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(trace_id) DO UPDATE SET
                     run_id = excluded.run_id, kind = excluded.kind, trace_json = excluded.trace_json""",
                (trace.trace_id, trace.run_id, trace.kind, json.dumps(trace.to_dict()), now),
            )
            connection.execute(
                "UPDATE runs SET updated_at = ? WHERE run_id = ?",
                (now, trace.run_id),
            )

    def get_traces(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT trace_json FROM traces WHERE run_id = ? ORDER BY created_at, trace_id",
                (run_id,),
            ).fetchall()
        return [json.loads(row["trace_json"]) for row in rows]

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown run: {run_id}")
        result = dict(row)
        result["config"] = json.loads(result.pop("config_json"))
        return result

    def find_latest_run(self, url: str) -> dict[str, Any] | None:
        """按稳定来源 URL 查找最近更新的 run。"""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE url = ? ORDER BY updated_at DESC LIMIT 1", (url,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["config"] = json.loads(result.pop("config_json"))
        return result

    def update_run_cost(self, run_id: str, amount_usd: float) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE runs SET cost_usd = cost_usd + ?, updated_at = ? WHERE run_id = ?",
                (amount_usd, _utc_now(), run_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"unknown run: {run_id}")

    def reserve_budget(self, run_id: str, token: str, amount_usd: float) -> bool:
        """在 LLM 请求前跨进程原子预留预算。"""
        amount = max(0.0, float(amount_usd))
        now = _utc_now()
        with self._connect() as connection:
            # 先锁定事务再汇总预留，否则两个 worker 可能同时看到同一余额并超支。
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT budget_usd, cost_usd FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(f"unknown run: {run_id}")
            reserved = connection.execute(
                "SELECT COALESCE(SUM(reserved_usd), 0) FROM reservations WHERE run_id = ? AND status = 'in_flight'",
                (run_id,),
            ).fetchone()[0]
            if float(run["cost_usd"]) + float(reserved) + amount > float(run["budget_usd"]) + 1e-12:
                return False
            connection.execute(
                "INSERT INTO reservations (token, run_id, reserved_usd, status, created_at, updated_at) VALUES (?, ?, ?, 'in_flight', ?, ?)",
                (token, run_id, amount, now, now),
            )
            return True

    def settle_reservation(self, run_id: str, token: str, actual_usd: float, result: dict[str, Any] | None = None) -> bool:
        """释放预留并原子记录供应商实际成本。"""
        actual = max(0.0, float(actual_usd))
        now = _utc_now()
        with self._connect() as connection:
            # 回复和成本在同一事务中持久化，结算后崩溃时可重建 finding，避免重复计费请求。
            connection.execute("BEGIN IMMEDIATE")
            reservation = connection.execute(
                "SELECT reserved_usd, status FROM reservations WHERE token = ? AND run_id = ?",
                (token, run_id),
            ).fetchone()
            if reservation is None or reservation["status"] != "in_flight":
                return False
            run = connection.execute("SELECT budget_usd, cost_usd FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            remaining = max(0.0, float(run["budget_usd"]) - float(run["cost_usd"]))
            accepted = actual <= remaining + 1e-12
            accounted = actual if accepted else remaining
            connection.execute("UPDATE runs SET cost_usd = cost_usd + ?, updated_at = ? WHERE run_id = ?", (accounted, now, run_id))
            connection.execute("UPDATE reservations SET status = ?, actual_usd = ?, result_json = ?, updated_at = ? WHERE token = ?", ("completed" if accepted else "rejected", actual, json.dumps(result or {}, ensure_ascii=True), now, token))
            return accepted

    def release_reservation(self, run_id: str, token: str) -> bool:
        """释放进行中的预算预留，但不记录供应商成本。"""
        now = _utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE reservations SET status = 'released', actual_usd = 0, updated_at = ? "
                "WHERE run_id = ? AND token = ? AND status = 'in_flight'",
                (now, run_id, token),
            )
            return cursor.rowcount > 0

    def update_run(self, run_id: str, *, status: str | None = None, cost_usd: float | None = None) -> None:
        updates: list[str] = []
        values: list[Any] = []
        if status is not None:
            updates.append("status = ?")
            values.append(status)
        if cost_usd is not None:
            updates.append("cost_usd = ?")
            values.append(cost_usd)
        if not updates:
            return
        updates.append("updated_at = ?")
        values.extend((_utc_now(), run_id))
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE runs SET {', '.join(updates)} WHERE run_id = ?", values
            )
            if cursor.rowcount == 0:
                raise KeyError(f"unknown run: {run_id}")
