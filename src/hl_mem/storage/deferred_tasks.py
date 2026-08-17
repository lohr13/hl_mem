"""由维护循环轮询的通用、有界待处理任务仓储。"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from hl_mem.storage._shared import decode_json, encode_json


class DeferredTaskRepository:
    """持久化跨越普通 job 终态的待处理工作。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def defer(
        self,
        *,
        task_type: str,
        resource_type: str,
        resource_id: str,
        payload: dict[str, Any],
        idempotency_key: str,
        run_after: str,
        max_attempts: int,
        error: str,
        updated_at: str,
    ) -> bool:
        """幂等登记待办；已有终态不会被旧失败重新打开。"""
        before = self.connection.total_changes
        self.connection.execute(
            "INSERT INTO deferred_tasks("
            "id,task_type,resource_type,resource_id,payload_json,idempotency_key,status,attempts,max_attempts,"
            "run_after,last_error,created_at,updated_at) VALUES(?,?,?,?,?,?,'pending',0,?,?,?,?,?) "
            "ON CONFLICT(idempotency_key) DO UPDATE SET "
            "last_error=excluded.last_error,updated_at=excluded.updated_at "
            "WHERE deferred_tasks.status='pending'",
            (
                uuid.uuid4().hex,
                task_type,
                resource_type,
                resource_id,
                encode_json(payload, sort_keys=True),
                idempotency_key,
                max_attempts,
                run_after,
                error[:2000],
                updated_at,
                updated_at,
            ),
        )
        self.connection.commit()
        return self.connection.total_changes > before

    def list_due(
        self,
        now: str,
        limit: int = 20,
        *,
        task_types: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        """返回到期且仍有重试预算的任务。"""
        type_filter = ""
        params: tuple[Any, ...] = (now,)
        if task_types:
            placeholders = ",".join("?" for _ in task_types)
            type_filter = f"AND task_type IN ({placeholders}) "
            params = (now, *task_types)
        rows = self.connection.execute(
            "SELECT * FROM deferred_tasks WHERE status='pending' AND attempts<max_attempts AND run_after<=? "
            f"{type_filter}ORDER BY run_after,created_at,id LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [self._decode(row) for row in rows]

    def list_exhausted(
        self,
        limit: int = 20,
        *,
        task_types: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        """返回预算已耗尽但尚未收敛终态的任务。"""
        type_filter = ""
        params: tuple[Any, ...] = ()
        if task_types:
            placeholders = ",".join("?" for _ in task_types)
            type_filter = f"AND task_type IN ({placeholders}) "
            params = task_types
        rows = self.connection.execute(
            "SELECT * FROM deferred_tasks WHERE status='pending' AND attempts>=max_attempts "
            f"{type_filter}ORDER BY updated_at,id LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [self._decode(row) for row in rows]

    def get(self, task_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM deferred_tasks WHERE id=?", (task_id,)).fetchone()
        return self._decode(row) if row is not None else None

    def get_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM deferred_tasks WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        return self._decode(row) if row is not None else None

    def record_attempt(
        self,
        task_id: str,
        *,
        run_after: str,
        updated_at: str,
        commit: bool = True,
    ) -> bool:
        cursor = self.connection.execute(
            "UPDATE deferred_tasks SET attempts=attempts+1,run_after=?,updated_at=? "
            "WHERE id=? AND status='pending' AND attempts<max_attempts",
            (run_after, updated_at, task_id),
        )
        if commit:
            self.connection.commit()
        return cursor.rowcount == 1

    def record_failure(
        self,
        task_id: str,
        *,
        run_after: str,
        error: str,
        updated_at: str,
    ) -> bool:
        """原子消耗一次重试预算并记录失败原因。"""
        cursor = self.connection.execute(
            "UPDATE deferred_tasks SET attempts=attempts+1,run_after=?,last_error=?,updated_at=? "
            "WHERE id=? AND status='pending' AND attempts<max_attempts",
            (run_after, error[:2000], updated_at, task_id),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def complete_task(self, task_id: str, updated_at: str, *, commit: bool = True) -> bool:
        """完成单个任务；可加入调用方业务事务。"""
        cursor = self.connection.execute(
            "UPDATE deferred_tasks SET status='completed',last_error=NULL,updated_at=? "
            "WHERE id=? AND status='pending'",
            (updated_at, task_id),
        )
        if commit:
            self.connection.commit()
        return cursor.rowcount == 1

    def postpone(self, task_id: str, run_after: str, error: str, updated_at: str) -> bool:
        cursor = self.connection.execute(
            "UPDATE deferred_tasks SET run_after=?,last_error=?,updated_at=? WHERE id=? AND status='pending'",
            (run_after, error[:2000], updated_at, task_id),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def complete_resources(self, resource_type: str, resource_ids: list[str], updated_at: str) -> int:
        return self._finish_resources(resource_type, resource_ids, "completed", None, updated_at)

    def abandon_resources(
        self,
        resource_type: str,
        resource_ids: list[str],
        error: str,
        updated_at: str,
    ) -> int:
        return self._finish_resources(resource_type, resource_ids, "abandoned", error, updated_at)

    def abandon_task(self, task_id: str, error: str, updated_at: str) -> bool:
        cursor = self.connection.execute(
            "UPDATE deferred_tasks SET status='abandoned',last_error=?,updated_at=? " "WHERE id=? AND status='pending'",
            (error[:2000], updated_at, task_id),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def _finish_resources(
        self,
        resource_type: str,
        resource_ids: list[str],
        status: str,
        error: str | None,
        updated_at: str,
    ) -> int:
        if not resource_ids:
            return 0
        placeholders = ",".join("?" for _ in resource_ids)
        cursor = self.connection.execute(
            f"UPDATE deferred_tasks SET status=?,last_error=?,updated_at=? "
            f"WHERE resource_type=? AND resource_id IN ({placeholders}) AND status='pending'",
            (status, error[:2000] if error is not None else None, updated_at, resource_type, *resource_ids),
        )
        self.connection.commit()
        return cursor.rowcount

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = decode_json(result.pop("payload_json"))
        return result
