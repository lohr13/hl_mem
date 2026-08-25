from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from hl_mem.application.deletion import DeletionRejectedError, DeletionService
from hl_mem.application.forget import ForgetService
from hl_mem.storage.database import Database
from hl_mem.storage.entities import EntityRepository
from hl_mem.storage.tombstones import TOMBSTONE_SCHEMA_VERSION, TombstoneLedger
from hl_mem.workers.repair_active_claims import audit_active_claims

NOW = "2026-08-15T00:00:00+00:00"
P0_STATUSES = ("active", "archived", "superseded")


@pytest.fixture
def deletion_store(tmp_path: Path):
    database_path = tmp_path / "memory.db"
    ledger_path = tmp_path / "memory.tombstones.db"
    database = Database(database_path, pool_size=1)
    connection = database.open()
    try:
        yield connection, ledger_path
    finally:
        database.close()


def _insert_event(connection: sqlite3.Connection, event_id: str) -> None:
    connection.execute(
        "INSERT INTO events(id,event_type,actor_type,content_json,occurred_at,recorded_at) "
        "VALUES (?, 'explicit_memory', 'user', ?, ?, ?)",
        (event_id, '{"text":"sensitive body"}', NOW, NOW),
    )


def _insert_claim(
    connection: sqlite3.Connection,
    claim_id: str,
    status: str,
    *,
    conflict_key: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO claims("
        "id,namespace_key,predicate,value_json,recorded_from,status,canonical_slot,conflict_key"
        ") VALUES (?, 'default', 'likes', ?, ?, ?, 'profile.fact', ?)",
        (claim_id, '"secret"', NOW, status, conflict_key),
    )


def _link_event(connection: sqlite3.Connection, claim_id: str, event_id: str) -> None:
    connection.execute(
        "INSERT INTO evidence_links("
        "id,derived_type,derived_id,evidence_type,evidence_id,relation"
        ") VALUES (?, 'claim', ?, 'event', ?, 'supports')",
        (f"link-{claim_id}-{event_id}", claim_id, event_id),
    )


def _service(connection: sqlite3.Connection, ledger_path: Path) -> DeletionService:
    return DeletionService(connection, ledger_path=ledger_path)


@pytest.mark.parametrize("status", P0_STATUSES)
@pytest.mark.parametrize("shared_event", (False, True))
def test_p0_delete_closure_preserves_only_shared_events(
    deletion_store,
    status: str,
    shared_event: bool,
) -> None:
    connection, ledger_path = deletion_store
    _insert_event(connection, "event-target")
    _insert_claim(connection, "target", status)
    _link_event(connection, "target", "event-target")
    if shared_event:
        _insert_claim(connection, "other", "active")
        _link_event(connection, "other", "event-target")
    connection.commit()

    result = _service(connection, ledger_path).delete_claim("target")

    assert result.deleted is True
    assert result.already_deleted is False
    assert connection.execute("SELECT 1 FROM claims WHERE id='target'").fetchone() is None
    event_exists = connection.execute("SELECT 1 FROM events WHERE id='event-target'").fetchone() is not None
    assert event_exists is shared_event
    if shared_event:
        assert connection.execute("SELECT 1 FROM claims WHERE id='other'").fetchone() is not None
    assert TombstoneLedger(ledger_path, create=False).count() == 1


def test_delete_claim_removes_entity_link_projection_and_proof(deletion_store) -> None:
    connection, ledger_path = deletion_store
    _insert_event(connection, "event-target")
    _insert_claim(connection, "target", "active")
    _link_event(connection, "target", "event-target")
    entities = EntityRepository(connection)
    entities.create_entity("agent:local_pony", "agent", "local_pony", "Local Pony", now=NOW)
    alias = entities.create_alias(" ＰＯＮＹ ", "agent", "agent:local_pony", "user_explicit", valid_from=NOW)
    entities.link_claim(
        "target",
        "agent:local_pony",
        "actor",
        mention_text="ＰＯＮＹ",
        resolution_confidence=1.0,
        alias_version=alias["version"],
        proof_id="link-target-event-target",
    )
    connection.commit()

    result = _service(connection, ledger_path).delete_claim("target")

    assert result.deleted is True
    assert connection.execute("SELECT 1 FROM claims WHERE id='target'").fetchone() is None
    assert connection.execute("SELECT 1 FROM claim_entity_links").fetchone() is None
    assert connection.execute("SELECT 1 FROM evidence_links").fetchone() is None
    assert connection.execute("SELECT 1 FROM events WHERE id='event-target'").fetchone() is None
    assert connection.execute("SELECT 1 FROM canonical_entities").fetchone() is not None
    assert connection.execute("SELECT 1 FROM entity_aliases").fetchone() is not None
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("status", P0_STATUSES)
@pytest.mark.parametrize("endpoint", ("from_id", "to_id"))
def test_p0_delete_closure_removes_relations_from_either_endpoint(
    deletion_store,
    status: str,
    endpoint: str,
) -> None:
    connection, ledger_path = deletion_store
    _insert_claim(connection, "target", status)
    _insert_claim(connection, "other", "active")
    from_id, to_id = ("target", "other") if endpoint == "from_id" else ("other", "target")
    connection.execute(
        "INSERT INTO memory_relations(id,from_id,to_id,relation,created_at) "
        "VALUES ('relation', ?, ?, 'supports', ?)",
        (from_id, to_id, NOW),
    )
    connection.commit()

    _service(connection, ledger_path).delete_claim("target")

    assert connection.execute("SELECT 1 FROM memory_relations WHERE id='relation'").fetchone() is None
    assert connection.execute("SELECT 1 FROM claims WHERE id='other'").fetchone() is not None


@pytest.mark.parametrize("status", P0_STATUSES)
def test_p0_bound_but_missing_ledger_fails_closed(deletion_store, status: str) -> None:
    connection, ledger_path = deletion_store
    _insert_claim(connection, "target", status)
    ledger = TombstoneLedger(ledger_path)
    connection.execute(
        "INSERT INTO deletion_ledger_state(" "singleton,ledger_id,schema_version,bound_at" ") VALUES (1,?,?,?)",
        (ledger.ledger_id, TOMBSTONE_SCHEMA_VERSION, NOW),
    )
    connection.commit()
    ledger_path.unlink()

    with pytest.raises(DeletionRejectedError, match="ledger_missing"):
        _service(connection, ledger_path).delete_claim("target")

    assert connection.execute("SELECT 1 FROM claims WHERE id='target'").fetchone() is not None


@pytest.mark.parametrize("status", ("candidate", "disputed", "expired"))
def test_ambiguous_statuses_fail_closed_with_reason(deletion_store, status: str) -> None:
    connection, ledger_path = deletion_store
    _insert_claim(connection, "target", status)
    connection.commit()

    with pytest.raises(DeletionRejectedError, match=f"status_{status}"):
        _service(connection, ledger_path).delete_claim("target")

    assert connection.execute("SELECT 1 FROM claims WHERE id='target'").fetchone() is not None
    assert not ledger_path.exists()


def test_expired_delete_entry_requires_no_evidence_consumers(deletion_store) -> None:
    connection, ledger_path = deletion_store
    _insert_claim(connection, "target", "expired")
    _insert_claim(connection, "consumer", "active")
    connection.execute(
        "INSERT INTO evidence_links(id,derived_type,derived_id,evidence_type,evidence_id,relation) "
        "VALUES ('consumer-link','claim','consumer','claim','target','derived_from')"
    )
    connection.commit()

    with pytest.raises(DeletionRejectedError, match="evidence_consumers"):
        _service(connection, ledger_path).delete_expired_claim("target")

    connection.execute("DELETE FROM evidence_links WHERE id='consumer-link'")
    connection.commit()
    result = _service(connection, ledger_path).delete_expired_claim("target")

    assert result.deleted is True
    assert connection.execute("SELECT 1 FROM claims WHERE id='target'").fetchone() is None


@pytest.mark.parametrize("case_status", ("pending", "auto_resolved", "manual_required"))
def test_open_conflict_case_fails_closed_with_reason(deletion_store, case_status: str) -> None:
    connection, ledger_path = deletion_store
    _insert_claim(connection, "target", "active")
    _insert_claim(connection, "other", "candidate")
    connection.execute(
        "INSERT INTO conflict_cases("
        "id,pair_key,left_claim_id,right_claim_id,status,created_at"
        ") VALUES ('case', 'pair', 'target', 'other', ?, ?)",
        (case_status, NOW),
    )
    connection.commit()

    with pytest.raises(DeletionRejectedError, match="open_conflict_case"):
        _service(connection, ledger_path).delete_claim("target")

    assert connection.execute("SELECT 1 FROM claims WHERE id='target'").fetchone() is not None
    assert not ledger_path.exists()


def test_retracted_claim_converges_once_then_replays_idempotently(deletion_store) -> None:
    connection, ledger_path = deletion_store
    _insert_claim(connection, "target", "retracted")
    connection.commit()

    first = _service(connection, ledger_path).delete_claim("target")
    second = _service(connection, ledger_path).delete_claim("target")

    assert first.deleted is True
    assert second.deleted is False
    assert second.already_deleted is True
    assert second.identity_hash == first.identity_hash
    assert TombstoneLedger(ledger_path, create=False).count() == 1


def test_sidecar_write_failure_rolls_back_main_database(deletion_store) -> None:
    connection, ledger_path = deletion_store
    _insert_event(connection, "event-target")
    _insert_claim(connection, "target", "active")
    _link_event(connection, "target", "event-target")
    connection.commit()
    ledger = TombstoneLedger(ledger_path)
    with sqlite3.connect(ledger_path) as sidecar:
        sidecar.execute(
            "CREATE TRIGGER fail_tombstone_write BEFORE INSERT ON tombstones "
            "BEGIN SELECT RAISE(ABORT, 'injected write failure'); END"
        )

    with pytest.raises(DeletionRejectedError, match="ledger_write_failed"):
        _service(connection, ledger_path).delete_claim("target")

    assert connection.execute("SELECT 1 FROM claims WHERE id='target'").fetchone() is not None
    assert connection.execute("SELECT 1 FROM events WHERE id='event-target'").fetchone() is not None
    assert connection.execute("SELECT 1 FROM deletion_ledger_state").fetchone() is None
    assert ledger.count() == 0


def test_delete_closure_removes_references_and_stales_derivation(deletion_store) -> None:
    connection, ledger_path = deletion_store
    _insert_event(connection, "event-target")
    _insert_claim(connection, "target", "active")
    _insert_claim(connection, "other", "active")
    _insert_claim(connection, "inbound", "superseded")
    _link_event(connection, "target", "event-target")
    connection.execute("UPDATE claims SET supersedes_id='target' WHERE id='other'")
    connection.execute("UPDATE claims SET superseded_by_id='target' WHERE id='inbound'")
    connection.execute(
        "INSERT INTO derivations(id,kind,body,status,updated_at) "
        "VALUES ('observation', 'observation', 'derived', 'active', ?)",
        (NOW,),
    )
    connection.execute(
        "INSERT INTO evidence_links("
        "id,derived_type,derived_id,evidence_type,evidence_id,relation"
        ") VALUES ('observation-link','observation','observation','claim','target','supports')"
    )
    connection.execute(
        "INSERT INTO memory_relations(id,from_id,to_id,relation,created_at) "
        "VALUES ('relation','target','other','supports',?)",
        (NOW,),
    )
    connection.execute(
        "INSERT INTO conflict_cases("
        "id,pair_key,left_claim_id,right_claim_id,status,created_at,resolved_at"
        ") VALUES ('case','pair','target','other','resolved',?,?)",
        (NOW, NOW),
    )
    connection.execute(
        "INSERT INTO relation_proposals("
        "id,run_id,source_claim_id,target_claim_id,relation,confidence,rationale,"
        "model,mode,status,relation_id,conflict_case_id,created_at"
        ") VALUES ("
        "'proposal','run','target','other','supports',0.9,'why','fake','audit',"
        "'applied','relation','case',?"
        ")",
        (NOW,),
    )
    connection.execute(
        "INSERT INTO dedup_pairs("
        "id,pair_key,left_claim_id,right_claim_id,similarity,created_at"
        ") VALUES ('dedup','dedup-pair','target','other',0.95,?)",
        (NOW,),
    )
    connection.execute(
        "INSERT INTO consolidation_pairs("
        "pair_key,embedding_signature,left_claim_id,right_claim_id,similarity,"
        "decision,run_id,reviewed_at"
        ") VALUES ('consolidation','signature','target','other',0.9,'distinct','run',?)",
        (NOW,),
    )
    connection.execute(
        "INSERT INTO memory_usefulness(memory_type,memory_id,updated_at) " "VALUES ('claim','target',?)",
        (NOW,),
    )
    connection.execute(
        "INSERT INTO retrieval_feedback("
        "id,query_id,memory_type,memory_id,created_at"
        ") VALUES ('feedback','query','claim','target',?)",
        (NOW,),
    )
    connection.execute("INSERT INTO claim_vector_dirty(claim_id,reason) VALUES ('target','update')")
    connection.commit()

    result = _service(connection, ledger_path).delete_claim("target")

    assert result.deleted_event_ids == ("event-target",)
    for table in (
        "memory_relations",
        "relation_proposals",
        "conflict_cases",
        "dedup_pairs",
        "consolidation_pairs",
        "memory_usefulness",
        "retrieval_feedback",
        "evidence_links",
        "claim_vector_dirty",
    ):
        assert connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is None
    assert connection.execute("SELECT status FROM derivations WHERE id='observation'").fetchone()[0] == "stale"
    assert connection.execute("SELECT supersedes_id FROM claims WHERE id='other'").fetchone()[0] is None
    assert connection.execute("SELECT superseded_by_id FROM claims WHERE id='inbound'").fetchone()[0] is None
    assert connection.execute("SELECT 1 FROM events WHERE id='event-target'").fetchone() is None
    assert audit_active_claims(connection)["dangling_references"]["total_count"] == 0


def test_archived_cleanup_reuses_closure_and_reports_rejections(deletion_store) -> None:
    connection, ledger_path = deletion_store
    _insert_claim(connection, "deletable", "archived")
    _insert_claim(connection, "blocked", "archived")
    _insert_claim(connection, "other", "active")
    _insert_claim(connection, "active-kept", "active")
    connection.execute(
        "INSERT INTO conflict_cases("
        "id,pair_key,left_claim_id,right_claim_id,status,created_at"
        ") VALUES ('case','pair','blocked','other','manual_required',?)",
        (NOW,),
    )
    connection.commit()

    report = _service(connection, ledger_path).cleanup_archived(limit=10)

    assert report.scanned == 2
    assert report.deleted == 1
    assert report.rejected == 1
    assert report.rejections == {"blocked": "open_conflict_case"}
    assert connection.execute("SELECT 1 FROM claims WHERE id='deletable'").fetchone() is None
    assert connection.execute("SELECT 1 FROM claims WHERE id='blocked'").fetchone() is not None
    assert connection.execute("SELECT 1 FROM claims WHERE id='active-kept'").fetchone() is not None


def test_forget_entry_uses_physical_delete_and_tombstone(deletion_store) -> None:
    connection, ledger_path = deletion_store
    _insert_claim(connection, "target", "active")
    connection.commit()

    result = ForgetService(connection, ledger_path=ledger_path).forget("target")

    assert result["forgotten"] is True
    assert result["already_deleted"] is False
    assert connection.execute("SELECT 1 FROM claims WHERE id='target'").fetchone() is None
    assert TombstoneLedger(ledger_path, create=False).count() == 1


def test_migration_binds_ledger_identity_and_records_delete_metadata(deletion_store) -> None:
    connection, ledger_path = deletion_store
    columns = {row[1] for row in connection.execute("PRAGMA table_info(deletion_ledger_state)").fetchall()}
    assert columns == {
        "singleton",
        "ledger_id",
        "schema_version",
        "bound_at",
        "last_identity_hash",
        "last_applied_at",
    }
    _insert_claim(connection, "target", "active")
    connection.commit()

    result = _service(connection, ledger_path).delete_claim("target")

    state = connection.execute(
        "SELECT ledger_id,schema_version,last_identity_hash,last_applied_at "
        "FROM deletion_ledger_state WHERE singleton=1"
    ).fetchone()
    assert state[0] == TombstoneLedger(ledger_path, create=False).ledger_id
    assert state[1] == TOMBSTONE_SCHEMA_VERSION
    assert state[2] == result.identity_hash
    assert datetime.fromisoformat(state[3]).tzinfo is not None


def test_migration_041_active_guard_allows_physical_delete(deletion_store) -> None:
    connection, ledger_path = deletion_store
    connection.execute(
        "INSERT INTO claims("
        "id,namespace_key,predicate,value_json,recorded_from,status,canonical_slot,conflict_key"
        ") VALUES ("
        "'target','default','uses','\"model-a\"',?,'active','config.model','model-choice'"
        ")",
        (NOW,),
    )
    connection.commit()
    trigger_count = connection.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='trigger' " "AND name LIKE 'claims_active_exclusive_guard_%'"
    ).fetchone()[0]

    _service(connection, ledger_path).delete_claim("target")

    assert trigger_count == 2
    assert connection.execute("SELECT 1 FROM claims WHERE id='target'").fetchone() is None
