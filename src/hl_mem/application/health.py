"""应用健康快照组装。"""

from __future__ import annotations

from hl_mem.monitoring.metrics import DEFAULT_PROVIDER_METRICS, ProviderMetrics
from hl_mem.monitoring.worker import DEFAULT_WORKER_RUNTIME, WorkerRuntimeState


def monitoring_snapshot(
    metrics: ProviderMetrics = DEFAULT_PROVIDER_METRICS,
    worker_runtime: WorkerRuntimeState = DEFAULT_WORKER_RUNTIME,
) -> dict[str, object]:
    """返回供 healthz 与 dashboard 消费的 provider 监控摘要。"""
    return {**metrics.snapshot(), "worker": worker_runtime.snapshot()}
