"""Write-path regressions for mutually exclusive active-claim groups."""

from __future__ import annotations

from dataclasses import replace

import pytest

from hl_mem.application.ingest import IngestService
from hl_mem.domain.claims.conflicts import compute_claim_pair_key
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from tests.unit._conflict_fixture import seed_pre_041_history

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


def _seed_group_member(
    connection,
    first_id: str,
    *,
    claim_id: str,
    value: str,
    status: str,
    predicate: str | None = None,
) -> str:
    first = ClaimRepository(connection).get_claim(first_id)
    assert first is not None
    assert ClaimRepository(connection).insert_claim(
        {
            "id": claim_id,
            "namespace_key": first["namespace_key"],
            "subject_entity_id": first["subject_entity_id"],
            "predicate": predicate or first["predicate"],
            "value": value,
            "qualifiers": first["qualifiers"],
            "canonical_attribute": first["canonical_attribute"],
            "canonical_slot": first["canonical_slot"],
            "fact_hash": f"seeded-hash-{claim_id}",
            "conflict_key": first["conflict_key"],
            "conflict_key_version": 3,
            "valid_from": NOW,
            "recorded_from": NOW,
            "observed_at": NOW,
            "status": status,
            "confidence": 0.9,
            "importance": 0.5,
            "scope": "permanent",
            "volatility": "stable",
            "source_authority": "medium",
        }
    )
    return claim_id


def _seed_second_active(connection, first_id: str, value: str = "8081") -> str:
    with seed_pre_041_history(connection):
        return _seed_group_member(
            connection,
            first_id,
            claim_id="seeded-dirty-active",
            value=value,
            status="active",
        )


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
        "SELECT id,left_claim_id,right_claim_id,status,decision,rationale,confidence,resolved_at "
        "FROM conflict_cases ORDER BY pair_key"
    ).fetchall()
    assert len(cases) == 2
    historical = next(row for row in cases if row["id"] == "seeded-resolved-case")
    assert tuple(historical[key] for key in ("status", "decision", "rationale", "confidence", "resolved_at")) == (
        "resolved",
        "coexist",
        "stale_resolution",
        0.9,
        NOW,
    )
    [new_case] = [row for row in cases if row is not historical]
    assert new_case["status"] == "manual_required"
    assert new_case["decision"] == "uncertain"
    assert new_case["rationale"] == "ingest_dirty_active_group"
    assert (
        connection.execute(
            "SELECT count(*) FROM conflict_case_candidates "
            "WHERE case_id=(SELECT id FROM conflict_cases WHERE status='manual_required')"
        ).fetchone()[0]
        == 3
    )


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


def test_ingest_without_conflict_group_inserts_one_active_claim(tmp_path) -> None:
    connection = Database(tmp_path / "no-group.db").open()

    result = _store(connection, _port("8080"), "event-first")

    assert result.claim_id is not None
    assert ClaimRepository(connection).get_claim(result.claim_id)["status"] == "active"
    assert connection.execute("SELECT count(*) FROM conflict_cases").fetchone()[0] == 0


def test_single_ambiguous_member_enters_manual_review_with_new_claim(tmp_path) -> None:
    connection = Database(tmp_path / "single-ambiguous.db").open()
    first = _store(connection, _port("8080"), "event-first")
    assert first.claim_id is not None

    result = _store(connection, _port("8081"), "event-ambiguous")

    rows = connection.execute("SELECT status FROM claims ORDER BY id").fetchall()
    assert [row["status"] for row in rows] == ["disputed", "disputed"]
    [case] = connection.execute("SELECT status,decision,rationale FROM conflict_cases").fetchall()
    assert tuple(case) == ("manual_required", "contradicts", "deterministic_ingest_resolution")
    assert result.claim_id is not None


def test_all_entailed_candidate_and_disputed_members_converge_to_one_active(tmp_path) -> None:
    connection = Database(tmp_path / "all-entailed.db").open()
    first = _store(connection, _port("8080"), "event-first")
    assert first.claim_id is not None
    connection.execute("UPDATE claims SET status='candidate' WHERE id=?", (first.claim_id,))
    connection.commit()
    second_id = _seed_group_member(
        connection,
        first.claim_id,
        claim_id="entailed-disputed",
        value="8080",
        status="disputed",
        predicate="使用",
    )

    result = _store(connection, _port("8080"), "event-entails-group")

    assert result.claim_id in {first.claim_id, second_id}
    rows = connection.execute("SELECT id,status,superseded_by_id FROM claims ORDER BY id").fetchall()
    assert sum(row["status"] == "active" for row in rows) == 1
    assert sum(row["status"] == "superseded" for row in rows) == 1
    loser = next(row for row in rows if row["status"] == "superseded")
    assert loser["superseded_by_id"] == result.claim_id
    assert connection.execute("SELECT count(*) FROM claims").fetchone()[0] == 2


def test_explicit_state_change_supersedes_every_nonterminal_group_member(tmp_path) -> None:
    connection = Database(tmp_path / "state-change-group.db").open()
    first = _store(connection, _port("8080"), "event-first")
    assert first.claim_id is not None
    candidate_id = _seed_group_member(
        connection,
        first.claim_id,
        claim_id="old-candidate",
        value="8081",
        status="candidate",
    )
    disputed_id = _seed_group_member(
        connection,
        first.claim_id,
        claim_id="old-disputed",
        value="8082",
        status="disputed",
    )

    changed = _port("9090")
    changed = replace(changed, qualifiers={**changed.qualifiers, "state_change": True})
    result = _store(connection, changed, "event-explicit-change")

    assert result.claim_id is not None
    rows = connection.execute("SELECT id,status,superseded_by_id FROM claims ORDER BY id").fetchall()
    assert [(row["id"], row["status"]) for row in rows if row["status"] == "active"] == [(result.claim_id, "active")]
    old_rows = [row for row in rows if row["id"] in {first.claim_id, candidate_id, disputed_id}]
    assert {row["status"] for row in old_rows} == {"superseded"}
    assert {row["superseded_by_id"] for row in old_rows} == {result.claim_id}


def test_mixed_candidate_and_disputed_group_is_quarantined_as_a_whole(tmp_path) -> None:
    connection = Database(tmp_path / "mixed-group.db").open()
    first = _store(connection, _port("8080"), "event-first")
    assert first.claim_id is not None
    connection.execute("UPDATE claims SET status='candidate' WHERE id=?", (first.claim_id,))
    connection.commit()
    rival_id = _seed_group_member(
        connection,
        first.claim_id,
        claim_id="mixed-disputed",
        value="8081",
        status="disputed",
    )

    result = _store(connection, _port("8080"), "event-mixed")

    assert result.claim_id == first.claim_id
    assert result.reason == "exact_duplicate_dirty_group"
    rows = connection.execute("SELECT id,status FROM claims ORDER BY id").fetchall()
    assert {row["id"] for row in rows} == {first.claim_id, rival_id}
    assert {row["status"] for row in rows} == {"disputed"}
    [case] = connection.execute("SELECT status,decision FROM conflict_cases").fetchall()
    assert tuple(case) == ("manual_required", "uncertain")


def test_uncertain_update_quarantines_active_candidate_and_new_claim(tmp_path) -> None:
    connection = Database(tmp_path / "uncertain-group.db").open()
    first = _store(connection, _port("8080"), "event-first")
    assert first.claim_id is not None
    candidate_id = _seed_group_member(
        connection,
        first.claim_id,
        claim_id="uncertain-candidate",
        value="8081",
        status="candidate",
    )
    connection.execute("UPDATE claims SET source_authority='high' WHERE id=?", (candidate_id,))
    connection.commit()

    result = _store(connection, _port("9090"), "event-uncertain")

    assert result.claim_id is not None
    rows = connection.execute("SELECT id,status FROM claims ORDER BY id").fetchall()
    assert {row["status"] for row in rows} == {"disputed"}
    assert {row["id"] for row in rows} == {first.claim_id, candidate_id, result.claim_id}
    cases = connection.execute("SELECT status,decision FROM conflict_cases").fetchall()
    assert [tuple(case) for case in cases] == [("manual_required", "uncertain")]
    assert connection.execute("SELECT count(*) FROM conflict_case_candidates").fetchone()[0] == 3


@pytest.mark.parametrize(
    ("slot", "qualifiers"),
    (
        ("config.path", {"purpose": "runtime"}),
        ("config.network", {"target": "api"}),
    ),
)
def test_nonexclusive_120_member_exact_replay_merges_evidence_without_case(
    tmp_path,
    slot: str,
    qualifiers: dict[str, str],
) -> None:
    connection = Database(tmp_path / f"nonexclusive-{slot}.db").open()
    extracted = ExtractedClaim(
        predicate="配置",
        value="value-000",
        subject="API服务",
        qualifiers=qualifiers,
        canonical_attribute=slot,
        canonical_slot=slot,
    )
    first = _store(connection, extracted, "event-first")
    assert first.claim_id is not None
    for index in range(1, 120):
        _seed_group_member(
            connection,
            first.claim_id,
            claim_id=f"member-{index:03d}",
            value=f"value-{index:03d}",
            status="active",
        )

    duplicate = _store(connection, extracted, "event-duplicate")

    assert duplicate.claim_id == first.claim_id
    assert duplicate.reason == "exact_duplicate"
    assert connection.execute("SELECT count(*) FROM claims").fetchone()[0] == 120
    assert connection.execute("SELECT count(*) FROM claims WHERE status='active'").fetchone()[0] == 120
    assert connection.execute("SELECT count(*) FROM conflict_cases").fetchone()[0] == 0
