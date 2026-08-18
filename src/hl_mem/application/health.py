"""应用健康快照组装。"""

from __future__ import annotations

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
