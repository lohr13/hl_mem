from __future__ import annotations

import json
from datetime import datetime, timezone

from hl_mem.observability.audit import (
    AuditLogger,
    NullAuditLogger,
    audit_context,
    audit_scope,
)
from hl_mem.storage.database import Database


def test_emit_writes_context_and_explicit_override(tmp_path) -> None:
    path = tmp_path / "audit.db"
    connection = Database(path).open()
    audit = AuditLogger(path)
    original = audit_context.get()
    with audit_scope(audit, trace_id="trace", tenant_id="context", event_id="event"):
        assert audit.emit(
            "filter",
            "evaluated",
            "allow",
            tenant_id="explicit",
            detail={"reason": "message"},
        )
    assert audit_context.get() == original
    row = connection.execute("SELECT * FROM audit_log").fetchone()
    assert row["trace_id"] == "trace" and row["tenant_id"] == "explicit"
    assert json.loads(row["detail_json"])["reason"] == "message"
    audit.close()


def test_emit_never_throws_and_reports_failure(tmp_path) -> None:
    audit = AuditLogger(tmp_path / "unmigrated.db")
    assert audit.emit("filter", "evaluated", "allow", trace_id="trace") is False
    assert audit.health()["dropped_count"] == 1
    assert audit.last_error
    audit.close()


def test_span_restores_context_and_records_error(tmp_path) -> None:
    path = tmp_path / "span.db"
    connection = Database(path).open()
    audit = AuditLogger(path)
    with audit_scope(audit, trace_id="span"):
        try:
            with audit.span("extraction", "evaluated"):
                raise ValueError("bad input")
        except ValueError:
            pass
    row = connection.execute("SELECT outcome,detail_json FROM audit_log").fetchone()
    assert row["outcome"] == "error"
    assert json.loads(row["detail_json"])["error_class"] == "ValueError"
    audit.close()


def test_null_audit_logger_is_noop() -> None:
    audit = NullAuditLogger()
    assert audit.emit("recall", "ranked", "disabled", trace_id="trace") is False
    assert audit.cleanup(30, batch_size=2) == {
        "deleted": 0,
        "remaining_expired": 0,
        "complete": True,
        "skipped": True,
    }
    assert audit.close() is True


def test_audit_cleanup_is_bounded_until_expired_backlog_is_drained(tmp_path) -> None:
    path = tmp_path / "cleanup.db"
    connection = Database(path).open()
    connection.executemany(
        "INSERT INTO audit_log(occurred_at,phase,action,outcome,trace_id,detail_json) "
        "VALUES (?,'test','test','success',?,'{}')",
        [("2025-01-01T00:00:00+00:00", f"old-{index}") for index in range(5)],
    )
    connection.execute(
        "INSERT INTO audit_log(occurred_at,phase,action,outcome,trace_id,detail_json) "
        "VALUES (?,'test','test','success','fresh','{}')",
        (datetime.now(timezone.utc).isoformat(),),
    )
    connection.commit()
    audit = AuditLogger(path)

    first = audit.cleanup(30, batch_size=2)
    second = audit.cleanup(30, batch_size=2)
    third = audit.cleanup(30, batch_size=2)
    fourth = audit.cleanup(30, batch_size=2)

    assert first == {"deleted": 2, "remaining_expired": 3, "complete": False, "skipped": False}
    assert second == {"deleted": 2, "remaining_expired": 1, "complete": False, "skipped": False}
    assert third == {"deleted": 1, "remaining_expired": 0, "complete": True, "skipped": False}
    assert fourth == {"deleted": 0, "remaining_expired": 0, "complete": True, "skipped": True}
    assert connection.execute("SELECT trace_id FROM audit_log").fetchone()[0] == "fresh"
    audit.close()


def test_audit_cleanup_remains_active_when_event_emission_is_disabled(tmp_path) -> None:
    path = tmp_path / "disabled-emission-cleanup.db"
    connection = Database(path).open()
    connection.execute(
        "INSERT INTO audit_log(occurred_at,phase,action,outcome,trace_id,detail_json) "
        "VALUES ('2025-01-01T00:00:00+00:00','test','test','success','old','{}')"
    )
    connection.commit()
    audit = AuditLogger(path, enabled=False)

    result = audit.cleanup(30, batch_size=2)

    assert result == {"deleted": 1, "remaining_expired": 0, "complete": True, "skipped": False}
    assert connection.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 0
    audit.close()
