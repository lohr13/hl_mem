"""LLM 调用 span 持久化。"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    """返回 UTC ISO 8601 时间。"""
    return datetime.now(timezone.utc).isoformat()


class LLMSpanRecorder:
    """记录单次 LLM 调用的 span。"""

    def __init__(self, connection: sqlite3.Connection | None = None) -> None:
        self._connection = connection
        self._enabled = connection is not None

    def record(
        self,
        *,
        operation: str,
        provider: str,
        model: str,
        status: str,
        latency_ms: float,
        started_at: str,
        structured_mode: str | None = None,
        attempt: int = 1,
        error_class: str | None = None,
        raw_request_id: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cached_tokens: int | None = None,
        total_tokens: int | None = None,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
    ) -> str | None:
        """记录一个 LLM 调用 span，返回 span_id。"""
        if not self._enabled or self._connection is None:
            return None
        span_id = uuid.uuid4().hex
        effective_trace = trace_id or span_id
        completed_at = _now_iso()
        self._connection.execute(
            """INSERT INTO llm_call_spans
               (span_id, parent_span_id, trace_id, operation, provider, model,
                structured_mode, attempt, status, error_class, raw_request_id,
                input_tokens, output_tokens, cached_tokens, total_tokens,
                latency_ms, started_at, completed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                span_id,
                parent_span_id,
                effective_trace,
                operation,
                provider,
                model,
                structured_mode,
                attempt,
                status,
                error_class,
                raw_request_id,
                input_tokens,
                output_tokens,
                cached_tokens,
                total_tokens,
                latency_ms,
                started_at,
                completed_at,
            ),
        )
        self._connection.commit()
        return span_id


def llm_span_stats(connection: sqlite3.Connection, since: str | None = None) -> dict[str, Any]:
    """聚合 LLM 调用统计。"""
    where = "WHERE started_at >= ?" if since else "WHERE 1=1"
    params: tuple[str, ...] = (since,) if since else ()
    rows = connection.execute(
        f"""SELECT operation, provider, model, status,
                  COUNT(*) as cnt,
                  COALESCE(SUM(total_tokens), 0) as tokens,
                  COALESCE(AVG(latency_ms), 0) as avg_latency
           FROM llm_call_spans {where}
           GROUP BY operation, provider, model, status
           ORDER BY operation, cnt DESC""",
        params,
    ).fetchall()
    return {
        "operations": [
            {
                "operation": row["operation"],
                "provider": row["provider"],
                "model": row["model"],
                "status": row["status"],
                "count": row["cnt"],
                "total_tokens": row["tokens"],
                "avg_latency_ms": round(row["avg_latency"], 1),
            }
            for row in rows
        ]
    }
