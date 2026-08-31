"""应用健康快照组装。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import TypedDict

from hl_mem.errors import OpsReportError
from hl_mem.monitoring.metrics import (
    DEFAULT_ADMISSION_METRICS,
    DEFAULT_PROVIDER_METRICS,
    AdmissionMetrics,
    ProviderMetrics,
)
from hl_mem.monitoring.worker import DEFAULT_WORKER_RUNTIME, WorkerRuntimeState
from hl_mem.recall.echo_suppression import (
    DEFAULT_ECHO_SUPPRESSION_METRICS,
    EchoSuppressionMetrics,
    EchoSuppressionMode,
)
from hl_mem.recall.freshness_annotation import (
    DEFAULT_FRESHNESS_ANNOTATION_METRICS,
    FreshnessAnnotationMetrics,
    FreshnessAnnotationMode,
)
from hl_mem.recall.injection import injection_governance_snapshot


def provider_usage_snapshot(runtime: object | None) -> dict[str, object] | None:
    """Keep the detailed usage counters and attach only today's cheap health aggregate."""
    if runtime is None:
        return None
    usage_snapshot = getattr(runtime, "usage_snapshot")()
    if usage_snapshot is None:
        return None
    try:
        health = getattr(runtime, "usage_health_snapshot")()
    except OpsReportError:
        health = None
    return {**usage_snapshot, "health": health}


class ConflictBacklogSnapshot(TypedDict):
    conflict_counts_by_status: dict[str, int]
    manual_required_count: int
    oldest_manual_required_age_seconds: int


def conflict_backlog_snapshot(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> ConflictBacklogSnapshot:
    """Return only unresolved conflict counts and the oldest residual manual age."""

    rows = connection.execute(
        "SELECT status,COUNT(*) AS count,MIN(created_at) AS oldest FROM conflict_cases "
        "WHERE status IN ('pending','auto_resolved','manual_required') AND resolved_at IS NULL GROUP BY status"
    ).fetchall()
    counts = {status: 0 for status in ("pending", "auto_resolved", "manual_required")}
    oldest_manual = None
    for row in rows:
        counts[str(row["status"])] = int(row["count"])
        if row["status"] == "manual_required":
            oldest_manual = row["oldest"]
    age = 0
    if oldest_manual:
        try:
            created = datetime.fromisoformat(str(oldest_manual).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age = max(0, int(((now or datetime.now(timezone.utc)) - created).total_seconds()))
        except ValueError:
            age = 0
    return {
        "conflict_counts_by_status": counts,
        "manual_required_count": counts["manual_required"],
        "oldest_manual_required_age_seconds": age,
    }


def monitoring_snapshot(
    metrics: ProviderMetrics = DEFAULT_PROVIDER_METRICS,
    worker_runtime: WorkerRuntimeState = DEFAULT_WORKER_RUNTIME,
    admission_metrics: AdmissionMetrics = DEFAULT_ADMISSION_METRICS,
    *,
    echo_metrics: EchoSuppressionMetrics = DEFAULT_ECHO_SUPPRESSION_METRICS,
    echo_mode: EchoSuppressionMode = "off",
    echo_session_window_seconds: int = 1800,
    echo_pending_review_enabled: bool = False,
    freshness_metrics: FreshnessAnnotationMetrics = DEFAULT_FRESHNESS_ANNOTATION_METRICS,
    freshness_mode: FreshnessAnnotationMode = "off",
) -> dict[str, object]:
    """返回供 healthz 与 dashboard 消费的 provider 监控摘要。"""
    injection = injection_governance_snapshot()
    injection["echo_suppression"] = echo_metrics.snapshot(
        mode=echo_mode,
        session_window_seconds=echo_session_window_seconds,
        pending_review_enabled=echo_pending_review_enabled,
    )
    injection["freshness_annotation"] = freshness_metrics.snapshot(mode=freshness_mode)
    return {
        **metrics.snapshot(),
        "admission": admission_metrics.snapshot(),
        "worker": worker_runtime.snapshot(),
        "injection_governance": injection,
    }
