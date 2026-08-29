"""Structured state lifecycle scorer tests against synthetic SQLite snapshots."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import hl_mem.evaluation.state_lifecycle as state_lifecycle
from hl_mem.evaluation.state_lifecycle import (
    compare_database_interval,
    compare_database_snapshots,
    main,
    open_readonly_database,
    score_database,
)
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database

T1 = "2026-01-01T00:00:00+00:00"
T1_HALF = "2026-01-15T00:00:00+00:00"
T2 = "2026-02-01T00:00:00+00:00"
T3 = "2026-03-01T00:00:00+00:00"


def _insert_claim(
    connection: sqlite3.Connection,
    claim_id: str,
    *,
    subject: str,
    value: str,
    valid_from: str,
    recorded_from: str,
    status: str = "active",
    predicate: str = "配置",
    canonical_attribute: str = "config.version",
    canonical_slot: str | None = None,
    qualifiers: dict[str, Any] | None = None,
    valid_to: str | None = None,
    recorded_to: str | None = None,
    supersedes_id: str | None = None,
    superseded_by_id: str | None = None,
) -> None:
    assert ClaimRepository(connection).insert_claim(
        {
            "id": claim_id,
            "namespace_key": "default",
            "subject_entity_id": subject,
            "predicate": predicate,
            "value": value,
            "qualifiers": qualifiers or {},
            "canonical_attribute": canonical_attribute,
            "canonical_slot": canonical_slot,
            "topic_tags_json": "[]",
            "fact_hash": f"hash-{claim_id}",
            "conflict_key": None,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "recorded_from": recorded_from,
            "recorded_to": recorded_to,
            "status": status,
            "scope": "permanent",
            "supersedes_id": supersedes_id,
            "superseded_by_id": superseded_by_id,
            "index_text": value,
        }
    )


def _insert_supersedes_evidence(
    connection: sqlite3.Connection,
    link_id: str,
    new_claim_id: str,
    old_claim_id: str,
) -> None:
    connection.execute(
        "INSERT INTO evidence_links(id,derived_type,derived_id,evidence_type,evidence_id,relation,weight) "
        "VALUES (?,'claim',?,'claim',?,'supersedes',1.0)",
        (link_id, new_claim_id, old_claim_id),
    )
    connection.commit()


def _structured_fixture(tmp_path: Path) -> Path:
    path = tmp_path / "structured-state.db"
    database = Database(path)
    connection = database.open()

    _insert_claim(
        connection,
        "x-old-active",
        subject="x",
        predicate="事实",
        value="X版本为0.1",
        valid_from=T1,
        recorded_from=T1,
        qualifiers={"state_change": True},
    )
    _insert_claim(
        connection,
        "x-new-active",
        subject="x",
        value="X版本为0.2",
        valid_from=T2,
        recorded_from=T2,
    )

    _insert_claim(
        connection,
        "y-old-closed",
        subject="y",
        value="Y版本为0.1",
        valid_from=T1,
        recorded_from=T1,
        valid_to=T2,
        recorded_to=T2,
        status="superseded",
    )
    _insert_claim(
        connection,
        "y-new",
        subject="y",
        value="Y版本为0.2",
        valid_from=T2,
        recorded_from=T2,
        supersedes_id="y-old-closed",
    )
    connection.execute("UPDATE claims SET superseded_by_id='y-new' WHERE id='y-old-closed'")
    connection.commit()
    _insert_supersedes_evidence(connection, "edge-y", "y-new", "y-old-closed")

    _insert_claim(
        connection,
        "p-old-open",
        subject="p",
        value="P版本为0.1",
        valid_from=T1,
        recorded_from=T1,
        recorded_to=T2,
        status="superseded",
    )
    _insert_claim(
        connection,
        "p-new",
        subject="p",
        value="P版本为0.2",
        valid_from=T2,
        recorded_from=T2,
    )
    _insert_supersedes_evidence(connection, "edge-p", "p-new", "p-old-open")

    _insert_claim(
        connection,
        "z-health",
        subject="z",
        predicate="状态",
        value="Z服务正常",
        valid_from=T2,
        recorded_from=T2,
        canonical_attribute="state.service_health",
        canonical_slot="state.service_health",
        qualifiers={"service": "Z"},
    )
    _insert_claim(
        connection,
        "non-state",
        subject="x",
        predicate="事实",
        value="这是一条普通事实",
        valid_from=T2,
        recorded_from=T2,
        canonical_attribute="fact.other",
    )

    connection.execute(
        "INSERT INTO audit_log(occurred_at,phase,action,outcome,trace_id,tenant_id,detail_json) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            T3,
            "conflict",
            "temporal_link",
            "snapshot_advance",
            "misleading-audit",
            "default",
            json.dumps(
                {
                    "outcome": "snapshot_advance",
                    "supersedes": ["invented-old", "invented-new"],
                }
            ),
        ),
    )
    connection.commit()
    database.close()
    return path


def _single_transition_fixture(tmp_path: Path, name: str, *, transitioned: bool) -> Path:
    path = tmp_path / name
    database = Database(path)
    connection = database.open()
    if transitioned:
        _insert_claim(
            connection,
            "old",
            subject="service",
            value="版本为0.1",
            valid_from=T1,
            recorded_from=T1,
            valid_to=T2,
            recorded_to=T2,
            status="superseded",
        )
        _insert_claim(
            connection,
            "new",
            subject="service",
            value="版本为0.2",
            valid_from=T2,
            recorded_from=T2,
            supersedes_id="old",
        )
        connection.execute("UPDATE claims SET superseded_by_id='new' WHERE id='old'")
        connection.commit()
        _insert_supersedes_evidence(connection, "edge", "new", "old")
    else:
        _insert_claim(
            connection,
            "old",
            subject="service",
            value="版本为0.1",
            valid_from=T1,
            recorded_from=T1,
        )
    database.close()
    return path


def test_score_uses_structured_coordinates_and_real_edges_only(tmp_path: Path) -> None:
    report = score_database(_structured_fixture(tmp_path), namespace="default", recorded_at=T3)

    assert report["coordinate_groups"]["summary"] == {
        "total": 4,
        "healthy": 3,
        "drifted": 1,
        "inactive": 0,
    }
    groups = {
        (
            item["coordinate"]["canonical_subject"],
            item["coordinate"]["canonical_slot"],
        ): item
        for item in report["coordinate_groups"]["groups"]
    }
    assert groups[("x", "config.version")]["active_count"] == 2
    assert groups[("x", "config.version")]["health"] == "drifted"
    assert groups[("z", "state.service_health")]["coordinate"]["coordinate_qualifiers"] == {"service": "z"}

    assert report["supersede_edges"] == {
        "total": 2,
        "sources": {
            "claims.superseded_by_id": 1,
            "claims.supersedes_id": 1,
            "evidence_links": 2,
        },
    }
    assert report["valid_to_closure"] == {"eligible": 2, "closed": 1, "rate": 0.5}
    assert report["current_state_stale_injection"] == {
        "active_surface": 5,
        "stale_active": 1,
        "rate": 0.2,
    }
    assert report["historical_old_snapshot_recall"] == {
        "covered_old": 3,
        "recallable": 3,
        "rate": 1.0,
    }
    # Claim mutation audit trigger (055/056) appends an audit row for the UPDATE above.
    assert report["diagnostics"]["audit_rows"] == 2


def test_compares_two_database_snapshots_with_numeric_deltas(tmp_path: Path) -> None:
    before = _single_transition_fixture(tmp_path, "before.db", transitioned=False)
    after = _single_transition_fixture(tmp_path, "after.db", transitioned=True)

    report = compare_database_snapshots(before, after, namespace="default")

    assert report["mode"] == "database_comparison"
    assert report["snapshots"]["before"]["supersede_edges"]["total"] == 0
    assert report["snapshots"]["after"]["supersede_edges"]["total"] == 1
    assert report["delta"]["supersede_edges.total"] == 1
    assert report["delta"]["valid_to_closure.rate"] == 1.0
    assert report["delta"]["current_state_stale_injection.rate"] == 0.0


def test_compares_two_recorded_cutoffs_in_one_database(tmp_path: Path) -> None:
    database_path = _single_transition_fixture(tmp_path, "interval.db", transitioned=True)

    report = compare_database_interval(
        database_path,
        before_at=T1_HALF,
        after_at=T3,
        namespace="default",
    )

    assert report["mode"] == "interval_comparison"
    assert report["snapshots"]["before"]["coordinate_groups"]["summary"]["healthy"] == 1
    assert report["snapshots"]["before"]["supersede_edges"]["total"] == 0
    assert report["snapshots"]["after"]["coordinate_groups"]["summary"]["healthy"] == 1
    assert report["snapshots"]["after"]["supersede_edges"]["total"] == 1


def test_readonly_cli_writes_report_without_changing_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = _structured_fixture(tmp_path)
    output_path = tmp_path / "reports" / "baseline.json"
    before_hash = hashlib.sha256(database_path.read_bytes()).hexdigest()
    before_stat = database_path.stat()

    connection = open_readonly_database(database_path)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        connection.execute("DELETE FROM claims")
    connection.close()

    assert (
        main(
            [
                "--db",
                str(database_path),
                "--namespace",
                "default",
                "--recorded-at",
                T3,
                "--output",
                str(output_path),
            ]
        )
        == 0
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    stdout = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "single"
    assert payload["snapshots"]["current"]["coordinate_groups"]["summary"]["drifted"] == 1
    assert stdout == {
        "coordinate_groups": 4,
        "drifted_groups": 1,
        "healthy_groups": 3,
        "mode": "single",
        "output": str(output_path),
        "supersede_edges": 2,
    }
    assert hashlib.sha256(database_path.read_bytes()).hexdigest() == before_hash
    after_stat = database_path.stat()
    assert (after_stat.st_size, after_stat.st_mtime_ns) == (before_stat.st_size, before_stat.st_mtime_ns)


def test_score_starts_one_read_snapshot_before_querying_claims(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = _single_transition_fixture(tmp_path, "transaction.db", transitioned=False)
    connection = open_readonly_database(database_path)
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    monkeypatch.setattr(state_lifecycle, "open_readonly_database", lambda _path: connection)

    score_database(database_path)

    begin_index = statements.index("BEGIN")
    claims_index = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith("SELECT ") and " FROM claims " in statement
    )
    assert begin_index < claims_index
