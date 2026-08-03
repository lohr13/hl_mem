"""线程安全的进程内 worker 运行状态。"""

from __future__ import annotations

import threading
from collections import Counter
from typing import Any


class WorkerRuntimeState:
    """记录当前进程内 worker 的存活信息与维护失败累计值。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False
        self._started_at: str | None = None
        self._stopped_at: str | None = None
        self._heartbeat_at: str | None = None
        self._maintenance_runs = 0
        self._maintenance_failures = 0
        self._last_maintenance_started_at: str | None = None
        self._last_maintenance_completed_at: str | None = None
        self._last_maintenance_error: str | None = None
        self._failure_counts: Counter[str] = Counter()

    def mark_started(self, at: str) -> None:
        with self._lock:
            self._running = True
            self._started_at = at
            self._stopped_at = None
            self._heartbeat_at = at

    def heartbeat(self, at: str) -> None:
        with self._lock:
            self._heartbeat_at = at

    def mark_stopped(self, at: str) -> None:
        with self._lock:
            self._running = False
            self._stopped_at = at
            self._heartbeat_at = at

    def begin_maintenance(self, at: str) -> None:
        with self._lock:
            self._maintenance_runs += 1
            self._last_maintenance_started_at = at
            self._last_maintenance_error = None
            self._heartbeat_at = at

    def record_maintenance_failure(self, item: str, error: Exception, at: str) -> None:
        with self._lock:
            self._maintenance_failures += 1
            self._failure_counts[item] += 1
            self._last_maintenance_error = f"{item}: {type(error).__name__}: {str(error)[:256]}"
            self._heartbeat_at = at

    def finish_maintenance(self, at: str) -> None:
        with self._lock:
            self._last_maintenance_completed_at = at
            self._heartbeat_at = at

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "started_at": self._started_at,
                "stopped_at": self._stopped_at,
                "heartbeat_at": self._heartbeat_at,
                "maintenance_runs": self._maintenance_runs,
                "maintenance_failures": self._maintenance_failures,
                "last_maintenance_started_at": self._last_maintenance_started_at,
                "last_maintenance_completed_at": self._last_maintenance_completed_at,
                "last_maintenance_error": self._last_maintenance_error,
                "failure_counts": dict(self._failure_counts),
            }


DEFAULT_WORKER_RUNTIME = WorkerRuntimeState()
