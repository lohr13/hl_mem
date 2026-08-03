"""Provider 监控和告警状态机测试。"""

from hl_mem.application.health import monitoring_snapshot
from hl_mem.monitoring.alerts import AlertManager, CircuitBreaker
from hl_mem.monitoring.metrics import ProviderCall, ProviderMetrics
from hl_mem.monitoring.worker import WorkerRuntimeState


class _Channel:
    def __init__(self) -> None:
        self.subjects: list[str] = []

    def send(self, subject: str, payload: dict[str, object]) -> None:
        self.subjects.append(subject)


def test_provider_metrics_keeps_last_one_hundred_calls() -> None:
    metrics = ProviderMetrics(100)
    for index in range(110):
        metrics.record(ProviderCall("llm", "extract", "success", float(index)))
    snapshot = metrics.snapshot()
    assert snapshot["calls"] == 100
    assert snapshot["p95_ms"] == 104.0


def test_alert_state_machine_deduplicates_and_recovers() -> None:
    channel = _Channel()
    manager = AlertManager([channel], dedupe_seconds=300)
    assert manager.transition("provider", "ERROR", {})
    assert not manager.transition("provider", "ERROR", {})
    assert manager.transition("provider", None, {})
    assert channel.subjects == ["ERROR: provider", "RECOVERED: provider"]


def test_circuit_breaker_opens_after_failures() -> None:
    breaker = CircuitBreaker(failure_threshold=2)
    breaker.record(False)
    assert breaker.allow()
    breaker.record(False)
    assert not breaker.allow()


def test_monitoring_snapshot_includes_worker_liveness() -> None:
    snapshot = monitoring_snapshot(ProviderMetrics(), WorkerRuntimeState())

    assert snapshot["worker"] == {
        "running": False,
        "started_at": None,
        "stopped_at": None,
        "heartbeat_at": None,
        "maintenance_runs": 0,
        "maintenance_failures": 0,
        "last_maintenance_started_at": None,
        "last_maintenance_completed_at": None,
        "last_maintenance_error": None,
        "failure_counts": {},
    }
