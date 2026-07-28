"""Provider 调用日报与长期 SLO 汇总。"""

from __future__ import annotations

import sqlite3
import time
from typing import Any


def generate_daily_report(connection: sqlite3.Connection, *, now: float | None = None) -> dict[str, Any]:
    """聚合最近 24 小时成功率、降级率和 P95 延迟。"""
    since = (now or time.time()) - 86400
    rows = connection.execute(
        "SELECT status,latency_ms,error_class,fallback FROM provider_calls WHERE recorded_at>=?",
        (since,),
    ).fetchall()
    latencies = sorted(float(row["latency_ms"]) for row in rows)
    calls = len(rows)
    p95 = latencies[min(calls - 1, max(0, int(calls * 0.95)))] if calls else 0.0
    return {
        "window_hours": 24,
        "calls": calls,
        "success_rate": sum(row["status"] == "success" for row in rows) / max(1, calls),
        "fallback_rate": sum(bool(row["fallback"]) for row in rows) / max(1, calls),
        "p95_ms": p95,
        "slo": {"success_rate_target": 0.99, "p95_ms_target": 30000.0},
    }
