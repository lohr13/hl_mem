"""尽力写入的 SQLite 审计日志与上下文绑定工具。"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

audit_context: ContextVar[dict[str, Any]] = ContextVar("audit_context", default={})
_audit_logger: ContextVar[Any] = ContextVar("audit_logger", default=None)
_SQLITE_AUDIT_DIMENSIONS = frozenset(
    {
        "trace_id",
        "tenant_id",
        "event_id",
        "related_claim_id",
        "query_id",
        "job_id",
        "claim_mutation_source",
    }
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_error(error: Exception) -> str:
    return str(error).replace("\n", " ")[:256]


def _json_default(value: Any) -> str:
    return repr(value)[:256]


class AuditLogger:
    """Best-effort synchronous SQLite audit writer.

    Audit failures are observable through ``health`` but never escape into the
    operational path.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        enabled: bool = True,
        max_detail_bytes: int = 16_384,
        busy_timeout_ms: int = 50,
    ) -> None:
        self.db_path = str(db_path)
        self.enabled = enabled
        self.max_detail_bytes = max_detail_bytes
        self.busy_timeout_ms = busy_timeout_ms
        self.dropped_count = 0
        self.emitted_count = 0
        self.written_count = 0
        self.last_error: str | None = None
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._last_cleanup_date: str | None = None

    def _open(self) -> sqlite3.Connection:
        if self._connection is None:
            connection = sqlite3.connect(self.db_path, check_same_thread=False)
            connection.execute(f"PRAGMA busy_timeout={max(0, int(self.busy_timeout_ms))}")
            self._connection = connection
        return self._connection

    def _detail_json(self, detail: Mapping[str, Any] | None) -> str:
        value = dict(detail or {})
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default)
        if len(serialized.encode("utf-8")) <= self.max_detail_bytes:
            return serialized
        compact = {"truncated": True, "original_bytes": len(serialized.encode("utf-8"))}
        return json.dumps(compact, separators=(",", ":"))

    def emit(
        self,
        phase: str,
        action: str,
        outcome: str,
        *,
        trace_id: str | None = None,
        tenant_id: str | None = None,
        event_id: str | None = None,
        claim_id: str | None = None,
        related_claim_id: str | None = None,
        query_id: str | None = None,
        job_id: str | None = None,
        duration_us: int | None = None,
        detail: Mapping[str, Any] | None = None,
        **dimensions: Any,
    ) -> bool:
        if not self.enabled:
            return False
        try:
            context = audit_context.get()
            values = dict(context)
            values.update({key: value for key, value in dimensions.items() if value is not None})
            explicit = {
                "trace_id": trace_id,
                "tenant_id": tenant_id,
                "event_id": event_id,
                "claim_id": claim_id,
                "related_claim_id": related_claim_id,
                "query_id": query_id,
                "job_id": job_id,
            }
            values.update({key: value for key, value in explicit.items() if value is not None})
            trace = str(
                values.get("trace_id")
                or values.get("event_id")
                or values.get("query_id")
                or values.get("job_id")
                or uuid.uuid4().hex
            )
            row = (
                _now(),
                str(phase),
                str(action),
                str(outcome),
                duration_us,
                trace,
                str(values.get("tenant_id", "default")),
                values.get("event_id"),
                values.get("claim_id"),
                values.get("related_claim_id"),
                values.get("query_id"),
                values.get("job_id"),
                self._detail_json(detail),
            )
            self.emitted_count += 1
            with self._lock:
                connection = self._open()
                connection.execute(
                    "INSERT INTO audit_log(occurred_at,phase,action,outcome,duration_us,"
                    "trace_id,tenant_id,event_id,claim_id,related_claim_id,query_id,job_id,detail_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    row,
                )
                connection.commit()
            self.written_count += 1
            self.last_error = None
            return True
        except Exception as error:
            self.dropped_count += 1
            self.last_error = f"{type(error).__name__}: {_safe_error(error)}"
            try:
                if self._connection is not None:
                    self._connection.rollback()
            except Exception as rollback_error:
                self.last_error += f"; rollback {type(rollback_error).__name__}: " f"{_safe_error(rollback_error)}"
            return False

    @contextmanager
    def span(self, phase: str, action: str, **dimensions: Any) -> Iterator[dict[str, Any]]:
        detail: dict[str, Any] = {}
        started = time.perf_counter_ns()
        try:
            yield detail
        except Exception as error:
            detail.update(error_class=type(error).__name__, error=_safe_error(error))
            self.emit(
                phase,
                action,
                "error",
                duration_us=(time.perf_counter_ns() - started) // 1000,
                detail=detail,
                **dimensions,
            )
            raise
        else:
            self.emit(
                phase,
                action,
                str(detail.pop("outcome", "success")),
                duration_us=(time.perf_counter_ns() - started) // 1000,
                detail=detail,
                **dimensions,
            )

    def cleanup(
        self,
        retention_days: int,
        *,
        batch_size: int = 2_000,
    ) -> dict[str, int | bool]:
        """每日有界删除过期审计；追平 backlog 前不标记当日完成。"""

        if retention_days < 1 or batch_size < 1:
            raise ValueError("audit retention days and batch size must be positive")
        today = datetime.now(timezone.utc).date().isoformat()
        if self._last_cleanup_date == today:
            return {"deleted": 0, "remaining_expired": 0, "complete": True, "skipped": True}
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
            with self._lock:
                connection = self._open()
                cursor = connection.execute(
                    "DELETE FROM audit_log WHERE id IN ("
                    "SELECT id FROM audit_log WHERE occurred_at<? ORDER BY occurred_at,id LIMIT ?)",
                    (cutoff, batch_size),
                )
                connection.commit()
                deleted = int(cursor.rowcount)
                remaining = int(
                    connection.execute(
                        "SELECT count(*) FROM audit_log WHERE occurred_at<?",
                        (cutoff,),
                    ).fetchone()[0]
                )
                complete = deleted < batch_size and remaining == 0
                if complete:
                    connection.execute("PRAGMA incremental_vacuum")
            if complete:
                self._last_cleanup_date = today
            return {
                "deleted": deleted,
                "remaining_expired": remaining,
                "complete": complete,
                "skipped": False,
            }
        except Exception as error:
            self.last_error = f"{type(error).__name__}: {_safe_error(error)}"
            try:
                if self._connection is not None:
                    self._connection.rollback()
            except Exception as rollback_error:
                self.last_error += f"; rollback {type(rollback_error).__name__}: " f"{_safe_error(rollback_error)}"
            return {"deleted": 0, "remaining_expired": 0, "complete": False, "skipped": False}

    def health(self) -> dict[str, int | bool | str | None]:
        return {
            "enabled": self.enabled,
            "emitted": self.emitted_count,
            "written": self.written_count,
            "dropped_count": self.dropped_count,
            "last_error": self.last_error,
        }

    def close(self) -> bool:
        try:
            with self._lock:
                if self._connection is not None:
                    self._connection.close()
                    self._connection = None
            return True
        except Exception as error:
            self.last_error = f"{type(error).__name__}: {_safe_error(error)}"
            return False


class NullAuditLogger:
    enabled = False
    dropped_count = 0
    last_error = None

    def emit(self, *args: Any, **kwargs: Any) -> bool:
        return False

    def cleanup(
        self,
        retention_days: int,
        *,
        batch_size: int = 2_000,
    ) -> dict[str, int | bool]:
        del retention_days, batch_size
        return {"deleted": 0, "remaining_expired": 0, "complete": True, "skipped": True}

    @contextmanager
    def span(self, *args: Any, **kwargs: Any) -> Iterator[dict[str, Any]]:
        yield {}

    def health(self) -> dict[str, int | bool | str | None]:
        return {
            "enabled": False,
            "emitted": 0,
            "written": 0,
            "dropped_count": 0,
            "last_error": None,
        }

    def close(self) -> bool:
        return True


_NULL_AUDIT = NullAuditLogger()


def current_audit() -> AuditLogger | NullAuditLogger:
    return _audit_logger.get() or _NULL_AUDIT


def current_audit_dimension(name: str) -> str | None:
    """Expose an allow-listed audit dimension to SQLite mutation triggers."""
    if name not in _SQLITE_AUDIT_DIMENSIONS:
        return None
    value = audit_context.get().get(name)
    return None if value is None else str(value)


@contextmanager
def audit_scope(logger: Any = None, **dimensions: Any) -> Iterator[None]:
    """Bind an audit logger and dimensions, restoring both ContextVar tokens."""
    merged = dict(audit_context.get())
    merged.update({key: value for key, value in dimensions.items() if value is not None})
    context_token = audit_context.set(merged)
    logger_token = _audit_logger.set(logger if logger is not None else current_audit())
    try:
        yield
    finally:
        _audit_logger.reset(logger_token)
        audit_context.reset(context_token)
