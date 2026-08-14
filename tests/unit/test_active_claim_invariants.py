"""Write-path regressions for mutually exclusive active-claim groups."""

from __future__ import annotations

from hl_mem.application.ingest import IngestService
from hl_mem.domain.claims.conflicts import compute_claim_pair_key
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database

NOW = "2026-08-14T08:00:00+00:00"


def _store(connection, claim: ExtractedClaim, event_id: str):
    return IngestService.store_extracted(
        connection,
        claim,
        {"id": event_id, "actor_type": "user", "tenant_id": "default"},
        NOW,
        FakeEmbedder(8),
    )


def _port(value: str) -> ExtractedClaim:
    return ExtractedClaim(
        predicate="配置",
        value=value,
        subject="API服务",
        qualifiers={"service": "gateway"},
        canonical_attribute="config.port",
        canonical_slot="config.port",
    )


def _seed_second_active(connection, first_id: str, value: str = "8081") -> str:
    first = ClaimRepository(connection).get_claim(first_id)
    assert first is not None
    second_id = "seeded-dirty-active"
    assert ClaimRepository(connection).insert_claim(
        {
            "id": second_id,
            "namespace_key": first["namespace_key"],
            "subject_entity_id": first["subject_entity_id"],
            "predicate": first["predicate"],
            "value": value,
            "qualifiers": first["qualifiers"],
            "canonical_attribute": first["canonical_attribute"],
            "canonical_slot": first["canonical_slot"],
            "fact_hash": f"seeded-hash-{value}",
            "conflict_key": first["conflict_key"],
            "conflict_key_version": 3,
            "valid_from": NOW,
            "recorded_from": NOW,
            "observed_at": NOW,
            "status": "active",
            "confidence": 0.9,
            "importance": 0.5,
            "scope": "permanent",
            "volatility": "stable",
            "source_authority": "medium",
        }
    )
    return second_id


def _seed_resolved_conflict_case(connection, left_id: str, right_id: str) -> None:
    connection.execute(
        "INSERT INTO conflict_cases("
        "id,pair_key,left_claim_id,right_claim_id,status,decision,rationale,confidence,created_at,resolved_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "seeded-resolved-case",
            compute_claim_pair_key(left_id, right_id),
            left_id,
            right_id,
            "resolved",
            "coexist",
            "stale_resolution",
            0.9,
            NOW,
            NOW,
        ),
    )
    connection.commit()


def test_ingest_quarantines_every_member_of_preexisting_dirty_active_group(tmp_path) -> None:
    connection = Database(tmp_path / "dirty-ingest.db").open()
    first = _store(connection, _port("8080"), "event-first")
    assert first.claim_id is not None
    second_id = _seed_second_active(connection, first.claim_id)
    _seed_resolved_conflict_case(connection, first.claim_id, second_id)

    result = _store(connection, _port("9090"), "event-new")

    assert result.claim_id is not None
    rows = connection.execute(
        "SELECT id,status FROM claims WHERE conflict_key=(SELECT conflict_key FROM claims WHERE id=?) ORDER BY id",
        (first.claim_id,),
    ).fetchall()
    assert {row["id"] for row in rows} == {first.claim_id, second_id, result.claim_id}
    assert {row["status"] for row in rows} == {"disputed"}
    cases = connection.execute(
        "SELECT left_claim_id,right_claim_id,status,decision,rationale,confidence,resolved_at "
        "FROM conflict_cases ORDER BY pair_key"
    ).fetchall()
    assert len(cases) == 3
    assert {frozenset((row["left_claim_id"], row["right_claim_id"])) for row in cases} == {
        frozenset((first.claim_id, second_id)),
        frozenset((first.claim_id, result.claim_id)),
        frozenset((second_id, result.claim_id)),
    }
    historical = next(
        row for row in cases if {row["left_claim_id"], row["right_claim_id"]} == {first.claim_id, second_id}
    )
    assert tuple(historical[key] for key in ("status", "decision", "rationale", "confidence", "resolved_at")) == (
        "resolved",
        "coexist",
        "stale_resolution",
        0.9,
        NOW,
    )
    new_cases = [row for row in cases if row is not historical]
    assert {row["status"] for row in new_cases} == {"manual_required"}
    assert {row["decision"] for row in new_cases} == {"uncertain"}
    assert {row["rationale"] for row in new_cases} == {"ingest_dirty_active_group"}


def test_exact_duplicate_also_quarantines_preexisting_dirty_active_group(tmp_path) -> None:
    connection = Database(tmp_path / "dirty-exact.db").open()
    first = _store(connection, _port("8080"), "event-first")
    assert first.claim_id is not None
    second_id = _seed_second_active(connection, first.claim_id)

    duplicate = _store(connection, _port("8080"), "event-duplicate")

    assert duplicate.claim_id == first.claim_id
    assert duplicate.reason == "exact_duplicate_dirty_group"
    assert connection.execute("SELECT count(*) FROM claims").fetchone()[0] == 2
    rows = connection.execute("SELECT id,status FROM claims ORDER BY id").fetchall()
    assert {row["id"] for row in rows} == {first.claim_id, second_id}
    assert {row["status"] for row in rows} == {"disputed"}
    conflict_case = connection.execute("SELECT status,decision,rationale FROM conflict_cases").fetchone()
    assert tuple(conflict_case) == ("manual_required", "uncertain", "ingest_dirty_active_group")


def test_clean_exact_duplicate_keeps_single_active_claim(tmp_path) -> None:
    connection = Database(tmp_path / "clean-exact.db").open()
    first = _store(connection, _port("8080"), "event-first")
    duplicate = _store(connection, _port("8080"), "event-second")

    assert duplicate.claim_id == first.claim_id
    assert duplicate.reason == "exact_duplicate"
    assert connection.execute("SELECT count(*) FROM claims WHERE status='active'").fetchone()[0] == 1


def test_clean_preference_state_change_preserves_existing_supersede_semantics(tmp_path) -> None:
    connection = Database(tmp_path / "clean-state-change.db").open()
    first = _store(
        connection,
        ExtractedClaim(
            predicate="偏好",
            value="深色模式",
            canonical_attribute="preference.ui_theme",
            canonical_slot="preference.ui_theme",
        ),
        "event-first",
    )
    second = _store(
        connection,
        ExtractedClaim(
            predicate="偏好",
            value="浅色模式",
            canonical_attribute="preference.ui_theme",
            canonical_slot="preference.ui_theme",
        ),
        "event-second",
    )

    assert first.claim_id is not None and second.claim_id is not None
    assert ClaimRepository(connection).get_claim(first.claim_id)["status"] == "superseded"
    assert ClaimRepository(connection).get_claim(second.claim_id)["status"] == "active"
