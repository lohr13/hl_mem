"""后台任务仓储。"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from hl_mem.storage._shared import decode_json, encode_json, insert_row, row_to_dict


class JobRepository:
    """提供任务写入、租约和终态更新。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def insert_job(self, job: dict[str, Any], commit: bool = True) -> bool:
        """写入后台任务。"""
        stored = dict(job)
        if "payload" in stored:
            stored["payload_json"] = encode_json(stored.pop("payload"), sort_keys=True)
        return insert_row(self.connection, "jobs", stored, commit)

    def lease_job(self, leased_until: str, updated_at: str) -> dict[str, Any] | None:
        """跨 worker 原子租用最早的可运行任务。"""
        lease_token = uuid.uuid4().hex
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT id FROM jobs WHERE (status='pending' OR (status='running' AND leased_until<?)) "
                "AND (run_after IS NULL OR run_after<=?) ORDER BY created_at,id LIMIT 1",
                (updated_at, updated_at),
            ).fetchone()
            if not row:
                self.connection.commit()
                return None
            cursor = self.connection.execute(
                "UPDATE jobs SET status='running',leased_until=?,updated_at=?,attempts=attempts+1,lease_token=? "
                "WHERE id=? AND (status='pending' OR (status='running' AND leased_until<?))",
                (leased_until, updated_at, lease_token, row["id"], updated_at),
            )
            self.connection.commit()
            if cursor.rowcount != 1:
                return None
            result = row_to_dict(self.connection.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())
            if result:
                result["lease_token"] = lease_token
                result["payload"] = decode_json(result["payload_json"])
            return result
        except Exception:
            self.connection.rollback()
            raise

    def complete_job(self, job_id: str, updated_at: str, lease_token: str) -> bool:
        """将当前租约任务标记为成功。"""
        return self._finish(job_id, "succeeded", updated_at, None, lease_token)

    def fail_job(self, job_id: str, error: str, updated_at: str, lease_token: str) -> bool:
        """记录任务失败，并按尝试次数决定重试或进入 dead。"""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT attempts,max_attempts FROM jobs WHERE id=? AND lease_token=? AND status='running'",
                (job_id, lease_token),
            ).fetchone()
            if not row:
                self.connection.commit()
                return False
            status = "dead" if row["attempts"] >= row["max_attempts"] else "pending"
            return self._finish(job_id, status, updated_at, error, lease_token)
        except Exception:
            self.connection.rollback()
            raise

    def force_finish_job(self, job_id: str, status: str, updated_at: str, error: str | None = None) -> bool:
        """管理员强制结束任务。"""
        cursor = self.connection.execute(
            "UPDATE jobs SET status=?,updated_at=?,last_error=?,leased_until=NULL,lease_token=NULL WHERE id=?",
            (status, updated_at, error, job_id),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def counts(self) -> dict[str, int]:
        """按状态统计任务。"""
        counts = {key: 0 for key in ("pending", "running", "failed", "dead")}
        rows = self.connection.execute("SELECT status,count(*) AS count FROM jobs GROUP BY status").fetchall()
        for row in rows:
            if row["status"] in counts:
                counts[row["status"]] = row["count"]
        return counts

    def _finish(self, job_id: str, status: str, updated_at: str, error: str | None, lease_token: str) -> bool:
        cursor = self.connection.execute(
            "UPDATE jobs SET status=?,updated_at=?,last_error=?,leased_until=NULL,lease_token=NULL "
            "WHERE id=? AND lease_token=? AND status='running'",
            (status, updated_at, error, job_id, lease_token),
        )
        self.connection.commit()
        return cursor.rowcount == 1
