from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from hl_mem.application.ingest import IngestService
from hl_mem.application.latest_wins import prepare_latest_wins
from hl_mem.domain.claims.attributes import SLOT_REGISTRY
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.observability.audit import AuditLogger, audit_scope
from hl_mem.settings import Settings
from hl_mem.state_latest_wins import CurrentnessProof
from hl_mem.storage.database import Database
from hl_mem.storage.events import EventRepository


def _settings(mode: str) -> Settings:
    return replace(Settings.for_test(), latest_wins_mode=mode)


def _event(event_id: str, occurred_at: str) -> dict[str, object]:
    return {
        "id": event_id,
        "tenant_id": "default",
        "event_type": "status_report",
        "actor_type": "tool",
        "content": {"schema_version": "status_report_v1"},
        "occurred_at": occurred_at,
        "recorded_at": occurred_at,
    }


def _proof(version: str, observed_at: str) -> CurrentnessProof:
    return CurrentnessProof(
        schema_version="status_report_v1",
        producer_contract="hl_mem.report-version-v1",
        package="hl_mem",
        runtime_version=version,
        namespace="default",
        canonical_entity_id="project:hl_mem",
        alias_version=1,
        observed_at=observed_at,
        producer_and_owner_verified=True,
    )


def _store_version(
    connection: object,
    event_id: str,
    version: str,
    observed_at: str,
    *,
    proof: CurrentnessProof | None,
    subject: str = "HL-Mem",
) -> str:
    event = _event(event_id, observed_at)
    EventRepository(connection).insert_event(event)  # type: ignore[arg-type]
    result = IngestService.store_extracted(
        connection,  # type: ignore[arg-type]
        ExtractedClaim(
            predicate=SLOT_REGISTRY["config.version"].predicate,
            value=version,
            subject=subject,
            canonical_attribute="config.version",
            canonical_slot="config.version",
            assertion_kind="observation",
        ),
        event,
        observed_at,
        FakeEmbedder(8),
        authority="high",
        currentness_proof=proof,
        _trusted_projector_slot="config.version",
    )
    assert result.claim_id is not None
    return result.claim_id


def test_exact_coordinate_query_fails_closed_on_conflict_key_collision(tmp_path: Path) -> None:
    database = Database(tmp_path / "coordinate-collision.db", settings=_settings("observe"))
    connection = database.open()
    other_id = _store_version(
        connection,
        "event-other",
        "0.31.1",
        "2026-08-26T01:00:00+00:00",
        proof=None,
        subject="Hermes",
    )
    incoming_id = _store_version(
        connection,
        "event-incoming",
        "0.32.0",
        "2026-08-26T02:00:00+00:00",
        proof=None,
    )
    incoming = dict(connection.execute("SELECT * FROM claims WHERE id=?", (incoming_id,)).fetchone())
    connection.execute("DELETE FROM evidence_links WHERE derived_id=?", (incoming_id,))
    connection.execute("DELETE FROM claims WHERE id=?", (incoming_id,))
    connection.execute("UPDATE claims SET conflict_key=? WHERE id=?", (incoming["conflict_key"], other_id))

    assert prepare_latest_wins(connection, incoming, (), None, mode="observe", slots=("config.version",)) is None


@pytest.mark.parametrize(
    ("mode", "old_status", "new_status", "latest_wins_audits", "last_action"),
    [
        ("off", "active", "active", 0, None),
        ("observe", "active", "active", 1, "suggested"),
        ("enforce", "superseded", "active", 1, "applied"),
    ],
)
def test_modes_control_audit_and_mutation_without_changing_the_relation(
    tmp_path: Path,
    mode: str,
    old_status: str,
    new_status: str,
    latest_wins_audits: int,
    last_action: str | None,
) -> None:
    database_path = tmp_path / f"{mode}.db"
    database = Database(database_path, settings=_settings(mode))
    connection = database.open()
    audit = AuditLogger(database_path)
    with audit_scope(audit):
        old_id = _store_version(
            connection,
            "event-old",
            "0.31.1",
            "2026-08-26T01:00:00+00:00",
            proof=_proof("0.31.1", "2026-08-26T01:00:00+00:00"),
        )
        new_id = _store_version(
            connection,
            "event-new",
            "0.30.0",
            "2026-08-26T02:00:00+00:00",
            proof=_proof("0.30.0", "2026-08-26T02:00:00+00:00"),
        )
    audit.close()

    statuses = dict(connection.execute("SELECT id,status FROM claims").fetchall())
    rows = connection.execute(
        "SELECT action,outcome,detail_json FROM audit_log WHERE phase='state_latest_wins' ORDER BY id"
    ).fetchall()
    assert statuses == {old_id: old_status, new_id: new_status}
    assert len(rows) == latest_wins_audits
    if rows:
        detail = json.loads(rows[-1]["detail_json"])
        assert (rows[-1]["action"], rows[-1]["outcome"]) == (last_action, "supersedes_existing")
        assert detail["schema_version"] == "state_latest_wins_audit_v1"
        assert detail["rule_id"] == "state-latest-wins-v1:supersedes_existing"
        assert detail["reason"] == "event_time_direction"


def test_enforce_merges_corroborating_evidence_without_growing_claim_revisions(tmp_path: Path) -> None:
    database = Database(tmp_path / "corroborates.db", settings=_settings("enforce"))
    connection = database.open()
    first_id = _store_version(
        connection,
        "event-a",
        "v0.31.1",
        "2026-08-26T01:00:00+00:00",
        proof=_proof("v0.31.1", "2026-08-26T01:00:00+00:00"),
    )
    second_id = _store_version(
        connection,
        "event-b",
        "0.31.1",
        "2026-08-26T02:00:00+00:00",
        proof=_proof("0.31.1", "2026-08-26T02:00:00+00:00"),
    )

    assert second_id == first_id
    assert connection.execute("SELECT count(*) FROM claims").fetchone()[0] == 1
    assert (
        connection.execute(
            "SELECT count(*) FROM evidence_links WHERE derived_type='claim' AND derived_id=?",
            (first_id,),
        ).fetchone()[0]
        == 2
    )


def test_historical_predecessor_does_not_close_the_current_tip(tmp_path: Path) -> None:
    database = Database(tmp_path / "predecessor.db", settings=_settings("enforce"))
    connection = database.open()
    current_id = _store_version(
        connection,
        "event-current",
        "0.31.1",
        "2026-08-26T03:00:00+00:00",
        proof=_proof("0.31.1", "2026-08-26T03:00:00+00:00"),
    )
    historical_id = _store_version(
        connection,
        "event-historical",
        "0.30.0",
        "2026-08-26T01:00:00+00:00",
        proof=_proof("0.30.0", "2026-08-26T01:00:00+00:00"),
    )

    statuses = dict(connection.execute("SELECT id,status FROM claims").fetchall())
    assert statuses == {current_id: "active", historical_id: "superseded"}
    assert (
        connection.execute("SELECT superseded_by_id FROM claims WHERE id=?", (historical_id,)).fetchone()[0]
        == current_id
    )


def test_missing_proof_keeps_both_claims_visible_and_creates_no_conflict_case(tmp_path: Path) -> None:
    database_path = tmp_path / "needs-review.db"
    database = Database(database_path, settings=_settings("enforce"))
    connection = database.open()
    audit = AuditLogger(database_path)
    with audit_scope(audit):
        _store_version(
            connection,
            "event-old",
            "0.31.1",
            "2026-08-26T01:00:00+00:00",
            proof=_proof("0.31.1", "2026-08-26T01:00:00+00:00"),
        )
        _store_version(
            connection,
            "event-chat",
            "0.32.0",
            "2026-08-26T02:00:00+00:00",
            proof=None,
        )
    audit.close()

    assert connection.execute("SELECT count(*) FROM claims WHERE status='active'").fetchone()[0] == 2
    assert connection.execute("SELECT count(*) FROM conflict_cases").fetchone()[0] == 0
    row = connection.execute(
        "SELECT outcome,detail_json FROM audit_log WHERE phase='state_latest_wins' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["outcome"] == "needs_review"
    assert json.loads(row["detail_json"])["reason"] == "currentness_proof_missing"


def test_non_target_claim_ledger_is_identical_in_all_three_modes(tmp_path: Path) -> None:
    ledgers = []
    for mode in ("off", "observe", "enforce"):
        database = Database(tmp_path / f"non-target-{mode}.db", settings=_settings(mode))
        connection = database.open()
        result = IngestService.store_extracted(
            connection,
            ExtractedClaim(
                predicate="fact",
                value="the non-target ledger stays stable",
                subject="HL-Mem",
                canonical_attribute="fact.implementation",
                canonical_slot="fact.implementation",
                assertion_kind="observation",
            ),
            {"id": "event-non-target", "tenant_id": "default", "actor_type": "user"},
            "2026-08-26T01:00:00+00:00",
            FakeEmbedder(8),
        )
        assert result.claim_id is not None
        row = dict(connection.execute("SELECT * FROM claims WHERE id=?", (result.claim_id,)).fetchone())
        ledgers.append({key: value for key, value in row.items() if key != "id"})

    assert ledgers[0] == ledgers[1] == ledgers[2]
