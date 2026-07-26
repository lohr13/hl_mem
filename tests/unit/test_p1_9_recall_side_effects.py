"""P1-9：召回副作用降级、重试和可观测性测试。"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import replace

import pytest

from hl_mem.application import recall as recall_module
from hl_mem.application.recall import RecallService, recall_side_effect_health
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.settings import Settings


def test_busy_access_side_effect_retries_without_breaking_recall(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """database busy 仅有限重试，最终失败仍记录日志和计数。"""
    connection = sqlite3.connect(":memory:")
    settings = replace(Settings.for_test(), recall_side_effect_max_attempts=3, recall_side_effect_backoff_seconds=0.001)
    service = RecallService(connection, FakeEmbedder(4), settings=settings)
    attempts = 0

    def fail_busy(*args: object, **kwargs: object) -> int:
        nonlocal attempts
        attempts += 1
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(recall_module.ClaimRepository, "record_access", fail_busy)
    monkeypatch.setattr(recall_module.time, "sleep", lambda _: None)
    before = recall_side_effect_health()["access_record"]["failures"]
    with caplog.at_level(logging.ERROR):
        service._record_access([{"id": "claim"}])

    assert attempts == 3
    assert recall_side_effect_health()["access_record"]["failures"] == before + 1
    assert "recall side effect failed" in caplog.text


def test_audit_failure_is_logged_and_counted(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """审计后端自身失败也不能再被静默吞掉。"""

    class BrokenAudit:
        def emit(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("audit unavailable")

    monkeypatch.setattr(recall_module, "current_audit", lambda: BrokenAudit())
    before = recall_side_effect_health()["audit_emit"]["failures"]
    with caplog.at_level(logging.ERROR):
        RecallService._emit_failure("feedback_record", "feedback_record_failed", RuntimeError("write"), 1)

    assert recall_side_effect_health()["audit_emit"]["failures"] == before + 1
    assert "recall failure audit emission failed" in caplog.text
