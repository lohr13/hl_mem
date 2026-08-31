"""后台任务仓储。"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from hl_mem.storage._shared import decode_json, encode_json, insert_row, row_to_dict


def _now_iso() -> str:
    """返回 UTC ISO 8601 时间。"""
    return datetime.now(timezone.utc).isoformat()


def _report_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _report_timestamp(value: object) -> str | None:
    parsed = _report_datetime(value)
    return None if parsed is None else parsed.isoformat()


def _safe_failure_category(value: object) -> str | None:
    """Classify a stored error without exposing its unbounded message."""
    if not isinstance(value, str) or not value:
        return None
    lowered = value.casefold()
    if "timeout" in lowered:
        return "timeout"
    if "locked" in lowered or "busy" in lowered:
        return "database_busy"
    if "connection" in lowered or "network" in lowered:
        return "connection"
    return "other"


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

    def lease_job(
        self,
        leased_until: str,
        updated_at: str,
        *,
        extraction_batch_max_events: int = 1,
        extraction_batch_max_wait_seconds: float = 0.0,
        force_extraction: bool = False,
    ) -> dict[str, Any] | None:
        """跨 worker 原子租用任务；可把同会话 Event job 合成有界窗口。"""
        if extraction_batch_max_events < 1:
            raise ValueError("extraction_batch_max_events must be positive")
        if extraction_batch_max_wait_seconds < 0:
            raise ValueError("extraction_batch_max_wait_seconds must be non-negative")
        lease_token = uuid.uuid4().hex
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            candidates = self.connection.execute(
                "SELECT j.*,e.id AS event_id,e.tenant_id AS event_tenant_id,"
                "e.session_id AS event_session_id,e.event_type AS source_event_type,"
                "e.recorded_at AS event_recorded_at "
                "FROM jobs j LEFT JOIN events e ON e.id=json_extract(j.payload_json,'$.event_id') "
                "WHERE (j.status='pending' OR (j.status='running' AND j.leased_until<?)) "
                "AND (j.run_after IS NULL OR j.run_after<=?) ORDER BY j.created_at,j.id LIMIT 1000",
                (updated_at, updated_at),
            ).fetchall()
            selected: list[sqlite3.Row] = []
            for candidate in candidates:
                selected = self._select_extraction_window(
                    candidate,
                    updated_at,
                    extraction_batch_max_events,
                    extraction_batch_max_wait_seconds,
                    force_extraction,
                )
                if selected:
                    break
            if not selected:
                self.connection.commit()
                return None
            job_ids = [str(row["id"]) for row in selected]
            placeholders = ",".join("?" for _ in job_ids)
            cursor = self.connection.execute(
                "UPDATE jobs SET status='running',leased_until=?,updated_at=?,attempts=attempts+1,lease_token=? "
                f"WHERE id IN ({placeholders}) "
                "AND (status='pending' OR (status='running' AND leased_until<?))",
                (leased_until, updated_at, lease_token, *job_ids, updated_at),
            )
            self.connection.commit()
            if cursor.rowcount != len(job_ids):
                return None
            result = row_to_dict(self.connection.execute("SELECT * FROM jobs WHERE id=?", (job_ids[0],)).fetchone())
            if result:
                result["lease_token"] = lease_token
                result["leased_job_ids"] = job_ids
                if result["job_type"] == "extract_event":
                    result["payload"] = {"event_ids": [str(row["event_id"]) for row in selected]}
                else:
                    result["payload"] = decode_json(result["payload_json"])
            return result
        except Exception:
            self.connection.rollback()
            raise

    def _select_extraction_window(
        self,
        candidate: sqlite3.Row,
        now: str,
        max_events: int,
        max_wait_seconds: float,
        force: bool,
    ) -> list[sqlite3.Row]:
        """返回候选 job 或其同 session 消息窗口；年轻窗口暂不租用。"""
        if (
            candidate["job_type"] != "extract_event"
            or max_events == 1
            or candidate["event_id"] is None
            or candidate["source_event_type"] != "message"
            or not candidate["event_session_id"]
        ):
            return [candidate]
        rows = self.connection.execute(
            "SELECT j.*,e.id AS event_id,e.tenant_id AS event_tenant_id,"
            "e.session_id AS event_session_id,e.event_type AS source_event_type,"
            "e.recorded_at AS event_recorded_at "
            "FROM jobs j JOIN events e ON e.id=json_extract(j.payload_json,'$.event_id') "
            "WHERE j.job_type='extract_event' "
            "AND (j.status='pending' OR (j.status='running' AND j.leased_until<?)) "
            "AND (j.run_after IS NULL OR j.run_after<=?) "
            "AND e.tenant_id=? AND e.session_id=? AND e.event_type='message' "
            "ORDER BY e.recorded_at,CASE e.actor_type WHEN 'user' THEN 0 WHEN 'assistant' THEN 1 ELSE 2 END,"
            "e.id,j.created_at,j.id LIMIT ?",
            (
                now,
                now,
                candidate["event_tenant_id"],
                candidate["event_session_id"],
                max_events,
            ),
        ).fetchall()
        if not rows:
            return [candidate]
        oldest = datetime.fromisoformat(str(rows[0]["event_recorded_at"]).replace("Z", "+00:00"))
        current = datetime.fromisoformat(now.replace("Z", "+00:00"))
        ready = force or len(rows) >= max_events or current >= oldest + timedelta(seconds=max_wait_seconds)
        return list(rows) if ready else []

    def complete_job(self, job_id: str, updated_at: str, lease_token: str) -> bool:
        """将当前租约任务标记为成功。"""
        return self.complete_jobs([job_id], updated_at, lease_token) == 1

    def complete_jobs(self, job_ids: list[str], updated_at: str, lease_token: str) -> int:
        """原子完成同一租约中的全部任务。"""
        return self._finish_many(job_ids, "succeeded", updated_at, None, lease_token)

    def fail_job(self, job_id: str, error: str, updated_at: str, lease_token: str) -> bool:
        """记录任务失败，并按尝试次数决定重试或进入 dead。"""
        return self.fail_jobs([job_id], error, updated_at, lease_token) == 1

    def fail_jobs(self, job_ids: list[str], error: str, updated_at: str, lease_token: str) -> int:
        """原子失败同一租约中的全部任务，并分别计算 retry/dead。"""
        if not job_ids:
            return 0
        placeholders = ",".join("?" for _ in job_ids)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            rows = self.connection.execute(
                f"SELECT id,attempts,max_attempts FROM jobs WHERE id IN ({placeholders}) "
                "AND lease_token=? AND status='running'",
                (*job_ids, lease_token),
            ).fetchall()
            for row in rows:
                status = "dead" if row["attempts"] >= row["max_attempts"] else "pending"
                self.connection.execute(
                    "UPDATE jobs SET status=?,updated_at=?,last_error=?,leased_until=NULL,lease_token=NULL "
                    "WHERE id=? AND lease_token=? AND status='running'",
                    (status, updated_at, error, row["id"], lease_token),
                )
            self.connection.commit()
            return len(rows)
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

    def report_snapshot(
        self,
        window: Any,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> dict[str, Any]:
        """Return content-free job and recall-side-effect health aggregates."""
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        current = now.astimezone(timezone.utc)
        statuses = {name: 0 for name in ("pending", "running", "succeeded", "failed", "dead")}
        for row in self.connection.execute("SELECT status,COUNT(*) AS count FROM jobs GROUP BY status").fetchall():
            status = str(row["status"])
            if status in statuses:
                statuses[status] = int(row["count"])
        types = {
            str(row["job_type"]): int(row["count"])
            for row in self.connection.execute(
                "SELECT job_type,COUNT(*) AS count FROM jobs GROUP BY job_type ORDER BY job_type"
            ).fetchall()
        }
        pending_timestamps = [
            parsed
            for row in self.connection.execute("SELECT created_at FROM jobs WHERE status='pending'").fetchall()
            if (parsed := _report_datetime(row["created_at"])) is not None
        ]
        oldest_pending_age_seconds = (
            max(0, int((current - min(pending_timestamps)).total_seconds())) if pending_timestamps else None
        )
        expired_running_leases = sum(
            1
            for row in self.connection.execute(
                "SELECT leased_until FROM jobs WHERE status='running' AND leased_until IS NOT NULL"
            ).fetchall()
            if (lease_until := _report_datetime(row["leased_until"])) is not None and lease_until <= current
        )
        heartbeat_timestamps = [
            parsed
            for row in self.connection.execute(
                "SELECT heartbeat_at FROM jobs WHERE heartbeat_at IS NOT NULL"
            ).fetchall()
            if (parsed := _report_datetime(row["heartbeat_at"])) is not None
        ]
        latest_heartbeat_at = max(heartbeat_timestamps).isoformat() if heartbeat_timestamps else None
        window_start, window_end = window.since.astimezone(timezone.utc), window.until.astimezone(timezone.utc)
        failures = [
            (updated_at, str(row["id"]), row["last_error"])
            for row in self.connection.execute(
                "SELECT id,updated_at,last_error FROM jobs WHERE status IN ('failed','dead')"
            ).fetchall()
            if (updated_at := _report_datetime(row["updated_at"])) is not None
            and window_start <= updated_at <= window_end
        ]
        last_failure = max(failures, default=(None, "", None), key=lambda item: (item[0], item[1]))
        recall_side_effect_backlog = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM deferred_tasks WHERE status='pending' "
                "AND task_type IN ('record_recall_access','record_recall_exposures')"
            ).fetchone()[0]
        )
        return {
            "counts_by_status": statuses,
            "counts_by_type": types,
            "failed_count": statuses["failed"],
            "dead_count": statuses["dead"],
            "oldest_pending_age_seconds": oldest_pending_age_seconds,
            "expired_running_leases": expired_running_leases,
            "last_safe_failure_category": _safe_failure_category(last_failure[2]),
            "latest_heartbeat_at": latest_heartbeat_at,
            "recall_side_effect_backlog": recall_side_effect_backlog,
        }

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        """按更新时间倒序返回任务及其进度。"""
        rows = self.connection.execute(
            "SELECT id,job_type,status,stage,processed,total,progress_detail_json,"
            "heartbeat_at,last_error,created_at,updated_at "
            "FROM jobs ORDER BY updated_at DESC,id LIMIT ?",
            (limit,),
        ).fetchall()
        jobs = [dict(row) for row in rows]
        for job in jobs:
            job["progress_detail"] = decode_json(job.pop("progress_detail_json"))
        return jobs

    def update_progress(
        self,
        job_id: str,
        lease_token: str,
        *,
        stage: str | None = None,
        processed: int | None = None,
        total: int | None = None,
        detail: dict[str, Any] | None = None,
        heartbeat_at: str | None = None,
    ) -> bool:
        """更新运行中任务的进度（需持有 lease token）。"""
        updates: list[str] = []
        params: list[Any] = []
        if stage is not None:
            updates.append("stage=?")
            params.append(stage)
        if processed is not None:
            updates.append("processed=?")
            params.append(processed)
        if total is not None:
            updates.append("total=?")
            params.append(total)
        if detail is not None:
            updates.append("progress_detail_json=?")
            params.append(encode_json(detail, sort_keys=True))
        if heartbeat_at is not None:
            updates.append("heartbeat_at=?")
            params.append(heartbeat_at)
        if not updates:
            return True
        updates.append("updated_at=?")
        params.append(heartbeat_at or _now_iso())
        params.extend([job_id, lease_token])
        cursor = self.connection.execute(
            f"UPDATE jobs SET {','.join(updates)} WHERE id=? AND lease_token=? AND status='running'",
            params,
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def renew_lease(
        self,
        job_ids: list[str],
        lease_token: str,
        *,
        leased_until: str,
        heartbeat_at: str,
    ) -> int:
        """Extend a running lease while preserving token-based ownership."""
        if not job_ids:
            return 0
        placeholders = ",".join("?" for _ in job_ids)
        cursor = self.connection.execute(
            "UPDATE jobs SET leased_until=?,heartbeat_at=?,updated_at=? "
            f"WHERE id IN ({placeholders}) AND lease_token=? AND status='running'",
            (leased_until, heartbeat_at, heartbeat_at, *job_ids, lease_token),
        )
        self.connection.commit()
        return cursor.rowcount

    def retry_failed(self) -> int:
        """将失败任务重置为待处理状态，由调用方提交事务。"""
        cursor = self.connection.execute("UPDATE jobs SET status='pending',last_error=NULL WHERE status='failed'")
        return cursor.rowcount

    def _finish(
        self,
        job_id: str,
        status: str,
        updated_at: str,
        error: str | None,
        lease_token: str,
    ) -> bool:
        return self._finish_many([job_id], status, updated_at, error, lease_token) == 1

    def _finish_many(
        self,
        job_ids: list[str],
        status: str,
        updated_at: str,
        error: str | None,
        lease_token: str,
    ) -> int:
        if not job_ids:
            return 0
        placeholders = ",".join("?" for _ in job_ids)
        cursor = self.connection.execute(
            "UPDATE jobs SET status=?,updated_at=?,last_error=?,leased_until=NULL,lease_token=NULL "
            f"WHERE id IN ({placeholders}) AND lease_token=? AND status='running'",
            (status, updated_at, error, *job_ids, lease_token),
        )
        self.connection.commit()
        return cursor.rowcount
