"""应用健康快照组装。"""

from __future__ import annotations

from hl_mem.monitoring.metrics import (
    DEFAULT_ADMISSION_METRICS,
    DEFAULT_PROVIDER_METRICS,
    AdmissionMetrics,
    ProviderMetrics,
)
from hl_mem.monitoring.worker import DEFAULT_WORKER_RUNTIME, WorkerRuntimeState


def monitoring_snapshot(
    metrics: ProviderMetrics = DEFAULT_PROVIDER_METRICS,
    worker_runtime: WorkerRuntimeState = DEFAULT_WORKER_RUNTIME,
    admission_metrics: AdmissionMetrics = DEFAULT_ADMISSION_METRICS,
) -> dict[str, object]:
    """返回供 healthz 与 dashboard 消费的 provider 监控摘要。"""
    return {
        **metrics.snapshot(),
        "admission": admission_metrics.snapshot(),
        "worker": worker_runtime.snapshot(),
    }
