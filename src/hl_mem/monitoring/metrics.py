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
    plugin_id: str | None = None
    provider: str | None = None
    model: str | None = None
    attempts: int = 1
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    embedding_items: int = 0
    rerank_documents: int = 0
    images: int = 0
    cost_microunits: int | None = None
    price_book_fingerprint: str | None = None


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
        providers = Counter(
            (call.plugin_id, call.provider, call.model)
            for call in calls
            if call.plugin_id is not None and call.provider is not None and call.model is not None
        )
        latencies = sorted(call.latency_ms for call in calls)

        def percentile(value: float) -> float:
            if not latencies:
                return 0.0
            return latencies[min(len(latencies) - 1, max(0, math.ceil(value * len(latencies)) - 1))]

        snapshot: dict[str, object] = {
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
            "attempts": sum(call.attempts for call in calls),
            "usage": {
                "requests": sum(call.requests for call in calls),
                "input_tokens": sum(call.input_tokens for call in calls),
                "output_tokens": sum(call.output_tokens for call in calls),
                "embedding_items": sum(call.embedding_items for call in calls),
                "rerank_documents": sum(call.rerank_documents for call in calls),
                "images": sum(call.images for call in calls),
                "cost_microunits": sum(call.cost_microunits or 0 for call in calls),
                "unknown_cost_calls": sum(call.cost_microunits is None and call.requests > 0 for call in calls),
            },
            "providers": [
                {"plugin_id": plugin_id, "provider": provider, "model": model, "calls": count}
                for (plugin_id, provider, model), count in sorted(providers.items())
            ],
        }
        fingerprints = {call.price_book_fingerprint for call in calls if call.price_book_fingerprint is not None}
        if len(fingerprints) == 1:
            snapshot["price_book_fingerprint"] = next(iter(fingerprints))
        elif fingerprints:
            snapshot["price_book_fingerprints"] = sorted(fingerprints)
        return snapshot


DEFAULT_PROVIDER_METRICS = ProviderMetrics()


class AdmissionMetrics:
    """记录摄入保护阈值拒绝的低基数进程级计数。"""

    def __init__(self) -> None:
        self._dedup_pending_pairs_skipped = 0
        self._lock = threading.Lock()

    def record_dedup_pending_pair_skipped(self) -> None:
        with self._lock:
            self._dedup_pending_pairs_skipped += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {"dedup_pending_pairs_skipped": self._dedup_pending_pairs_skipped}


DEFAULT_ADMISSION_METRICS = AdmissionMetrics()


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
