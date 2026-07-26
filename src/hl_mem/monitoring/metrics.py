"""Provider logical-call 的线程安全滑动窗口。"""

from __future__ import annotations

import math
import sqlite3
import threading
import time
from collections import Counter, deque
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ProviderCall:
    """一次逻辑 provider 调用的稳定诊断事件。"""

    provider_type: str
    operation: str
    status: str
    latency_ms: float
    query_id: str | None = None
    error_class: str | None = None
    http_status: int | None = None
    provider_code: str | None = None
    fallback: bool = False
    timestamp: float = 0.0


class ProviderMetrics:
    """保留最近固定数量调用并生成健康快照。"""

    def __init__(self, max_calls: int = 100) -> None:
        self._calls: deque[ProviderCall] = deque(maxlen=max_calls)
        self._lock = threading.Lock()

    def record(self, call: ProviderCall) -> None:
        """记录调用；未提供时间时使用当前单调无关的 Unix 时间。"""
        if call.timestamp == 0.0:
            call = ProviderCall(**{**asdict(call), "timestamp": time.time()})
        with self._lock:
            self._calls.append(call)

    def snapshot(self) -> dict[str, object]:
        """返回 failure/timeout/quota/fallback 与延迟分位数摘要。"""
        with self._lock:
            calls = list(self._calls)
        counts = Counter(call.status for call in calls)
        errors = Counter(call.error_class for call in calls if call.error_class)
        latencies = sorted(call.latency_ms for call in calls)

        def percentile(value: float) -> float:
            if not latencies:
                return 0.0
            return latencies[min(len(latencies) - 1, max(0, math.ceil(value * len(latencies)) - 1))]

        return {
            "calls": len(calls),
            "failures": sum(call.status != "success" for call in calls),
            "timeouts": errors["http_timeout"] + errors["deadline_timeout"],
            "quota": errors["quota"] + errors["rate_limit"],
            "fallbacks": sum(call.fallback for call in calls),
            "p50_ms": percentile(0.50),
            "p95_ms": percentile(0.95),
            "p99_ms": percentile(0.99),
            "status_counts": dict(counts),
            "error_counts": dict(errors),
        }


DEFAULT_PROVIDER_METRICS = ProviderMetrics()


class PersistentProviderMetrics(ProviderMetrics):
    """以内存窗口加 SQLite 事件形成跨进程 SSOT。"""

    def __init__(self, connection: sqlite3.Connection, max_calls: int = 100) -> None:
        super().__init__(max_calls)
        self.connection = connection

    def record(self, call: ProviderCall) -> None:
        """同时写入内存窗口与 provider_calls 表。"""
        if call.timestamp == 0.0:
            call = ProviderCall(**{**asdict(call), "timestamp": time.time()})
        super().record(call)
        self.connection.execute(
            "INSERT INTO provider_calls(provider_type,operation,status,latency_ms,query_id,error_class,http_status,provider_code,fallback,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                call.provider_type,
                call.operation,
                call.status,
                call.latency_ms,
                call.query_id,
                call.error_class,
                call.http_status,
                call.provider_code,
                int(call.fallback),
                call.timestamp,
            ),
        )
        self.connection.commit()
