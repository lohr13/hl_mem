"""disputed claim 状态修复脚本测试。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from hl_mem.storage.database import Database
from scripts.clean_disputed_claims import apply_cleanup, build_plan, write_report


def _insert_claim(
    connection: sqlite3.Connection,
    claim_id: str,
    conflict_key: str,
    *,
    status: str = "disputed",
    valid_to: str | None = None,
    expires_at: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO claims(id,namespace_key,subject_entity_id,predicate,value_json,conflict_key,"
        "recorded_from,status,canonical_attribute,conflict_key_version,valid_to,expires_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            claim_id,
            "default",
            "项目",
            "事实",
            json.dumps(claim_id),
            conflict_key,
            "2026-07-20T00:00:00+00:00",
            status,
            "fact.implementation",
            2,
            valid_to,
            expires_at,
        ),
    )


def _fixture(path: Path) -> sqlite3.Connection:
    connection = Database(path).open()
    _insert_claim(connection, "eligible", "only-disputed")
    _insert_claim(connection, "contested-a", "contested")
    _insert_claim(connection, "contested-b", "contested")
    _insert_claim(connection, "active-peer", "active-peer-key", status="active")
    _insert_claim(connection, "blocked-by-active", "active-peer-key")
    _insert_claim(connection, "expired-validity", "expired-validity-key", valid_to="2026-07-21T00:00:00+00:00")
    _insert_claim(connection, "expired-ttl", "expired-ttl-key", expires_at="2026-07-21T00:00:00+00:00")
    connection.execute(
        "INSERT INTO evidence_links(id,derived_type,derived_id,evidence_type,evidence_id,relation) "
        "VALUES ('e1','claim','eligible','event','event-1','supports')"
    )
    connection.commit()
    return connection


def test_build_plan_only_selects_current_disputed_without_live_peer(tmp_path: Path) -> None:
    connection = _fixture(tmp_path / "plan.db")

    plan = build_plan(connection, now="2026-07-22T00:00:00+00:00")

    assert plan.eligible_ids == ("eligible",)
    assert plan.disputed_count == 6
    assert plan.skipped_reasons == {
        "expired": 2,
        "live_peer": 3,
    }


def test_apply_only_changes_status_and_preserves_evidence(tmp_path: Path) -> None:
    database_path = tmp_path / "apply.db"
    connection = _fixture(database_path)
    before_claims = {
        row["id"]: dict(row)
        for row in connection.execute("SELECT * FROM claims ORDER BY id")
    }
    before_evidence = [tuple(row) for row in connection.execute("SELECT * FROM evidence_links ORDER BY id")]
    plan = build_plan(connection, now="2026-07-22T00:00:00+00:00")
    connection.close()
    backup_path = tmp_path / "backup.db"

    result = apply_cleanup(database_path, backup_path, plan)

    assert result.updated_count == 1
    applied = sqlite3.connect(database_path)
    applied.row_factory = sqlite3.Row
    after_claims = {row["id"]: dict(row) for row in applied.execute("SELECT * FROM claims ORDER BY id")}
    for claim_id, before in before_claims.items():
        expected = dict(before)
        if claim_id == "eligible":
            expected["status"] = "active"
        assert after_claims[claim_id] == expected
    assert [tuple(row) for row in applied.execute("SELECT * FROM evidence_links ORDER BY id")] == before_evidence
    backup = sqlite3.connect(backup_path)
    assert backup.execute("SELECT status FROM claims WHERE id='eligible'").fetchone()[0] == "disputed"


def test_write_report_omits_claim_values(tmp_path: Path) -> None:
    connection = _fixture(tmp_path / "report.db")
    plan = build_plan(connection, now="2026-07-22T00:00:00+00:00")
    report_path = tmp_path / "report.json"

    write_report(report_path, plan, result=None)

    report_text = report_path.read_text(encoding="utf-8")
    assert '"eligible"' in report_text
    assert "value_json" not in report_text
