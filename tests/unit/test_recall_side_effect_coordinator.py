from __future__ import annotations

import logging
import sqlite3
from dataclasses import replace

import pytest

from hl_mem.application.recall_side_effects import RecallSideEffectCoordinator
from hl_mem.settings import Settings


class _Sink:
    def __init__(self, *, accepted: bool = True) -> None:
        self.accepted = accepted
        self.access: list[tuple[str, list[str], str]] = []
        self.exposures: list[tuple[str, list[tuple[object, ...]]]] = []

    def submit_access(self, query_id: str, claim_ids: list[str], accessed_at: str) -> bool:
        self.access.append((query_id, claim_ids, accessed_at))
        return self.accepted

    def submit_exposures(self, query_id: str, exposures: list[tuple[object, ...]]) -> bool:
        self.exposures.append((query_id, exposures))
        return self.accepted


def _coordinator(connection: sqlite3.Connection, sink: _Sink | None = None, *, sleep=lambda _: None):
    return RecallSideEffectCoordinator(
        connection,
        replace(
            Settings.for_test(),
            recall_side_effect_max_attempts=3,
            recall_side_effect_backoff_seconds=0.1,
        ),
        sink,
        now=lambda: "2026-09-01T00:00:00+00:00",
        sleep=sleep,
        audit_provider=lambda: None,
        failure_recorder=lambda _operation, _error: None,
        logger=logging.getLogger(__name__),
    )


def test_coordinator_retries_busy_operations_with_existing_linear_backoff() -> None:
    connection = sqlite3.connect(":memory:")
    delays: list[float] = []
    attempts = 0

    def operation(received: sqlite3.Connection) -> str:
        nonlocal attempts
        assert received is connection
        attempts += 1
        if attempts < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    result = _coordinator(connection, sleep=delays.append).run_with_retry(operation)

    assert result == "ok"
    assert attempts == 3
    assert delays == [0.1, 0.2]
    connection.close()


def test_coordinator_preserves_sink_acceptance_contract() -> None:
    connection = sqlite3.connect(":memory:")
    sink = _Sink()
    coordinator = _coordinator(connection, sink)
    exposures = [("feedback-1", "query-1", "claim", "claim-1", 1, 0.9, "now")]

    assert coordinator.submit_exposures("query-1", exposures) == 1
    coordinator.submit_access("query-1", [{"id": "claim-1"}])

    assert sink.exposures == [("query-1", exposures)]
    assert sink.access == [("query-1", ["claim-1"], "2026-09-01T00:00:00+00:00")]
    connection.close()


def test_coordinator_rejects_exposures_when_sink_is_missing_or_rejects() -> None:
    connection = sqlite3.connect(":memory:")
    exposures = [("feedback-1", "query-1", "claim", "claim-1", 1, 0.9, "now")]

    with pytest.raises(RuntimeError, match="exposure submission rejected"):
        _coordinator(connection).submit_exposures("query-1", exposures)
    with pytest.raises(RuntimeError, match="exposure submission rejected"):
        _coordinator(connection, _Sink(accepted=False)).submit_exposures("query-1", exposures)
    connection.close()
