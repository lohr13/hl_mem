from __future__ import annotations

import json

from hl_mem.observability.audit import AuditLogger, NullAuditLogger, audit_context, audit_scope
from hl_mem.storage.database import Database


def test_emit_writes_context_and_explicit_override(tmp_path) -> None:
    path = tmp_path / "audit.db"
    connection = Database(path).open()
    audit = AuditLogger(path)
    original = audit_context.get()
    with audit_scope(audit, trace_id="trace", tenant_id="context", event_id="event"):
        assert audit.emit("filter", "evaluated", "allow", tenant_id="explicit",
                          detail={"reason": "message"})
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
    assert audit.close() is True
