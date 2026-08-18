from __future__ import annotations

from hl_mem.application.conflicts import ResolutionService
from hl_mem.application.ingest import IngestService
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database

NOW = "2026-08-18T08:00:00+00:00"


def _port(value: str, *, state_change: bool = False) -> ExtractedClaim:
    qualifiers: dict[str, object] = {"service": "gateway"}
    if state_change:
        qualifiers["state_change"] = True
    return ExtractedClaim(
        predicate="配置",
        value=value,
        subject="gateway",
        qualifiers=qualifiers,
        canonical_attribute="config.port",
        canonical_slot="config.port",
    )


def _store(connection, value: str, event_id: str, *, state_change: bool = False):
    return IngestService.store_extracted(
        connection,
        _port(value, state_change=state_change),
        {"id": event_id, "actor_type": "user", "tenant_id": "default"},
        NOW,
        FakeEmbedder(8),
    )


def _resolved_generation_one(tmp_path):
    connection = Database(tmp_path / "generation-ingest.db").open()
    first = _store(connection, "8080", "event-8080")
    second = _store(connection, "8081", "event-8081")
    assert first.claim_id is not None and second.claim_id is not None
    case = connection.execute(
        "SELECT id,generation,revision,status FROM conflict_cases WHERE group_key IS NOT NULL"
    ).fetchone()
    assert tuple(case[key] for key in ("generation", "status")) == (1, "manual_required")
    ResolutionService(connection).resolve_group(
        case["id"],
        "select_candidate",
        candidate_key='"8080"',
        expected_revision=case["revision"],
        resolved_at=NOW,
    )
    return connection, str(first.claim_id), str(case["id"])


def test_same_value_after_terminal_generation_adds_evidence_without_reopening(tmp_path) -> None:
    connection, winner_id, terminal_case_id = _resolved_generation_one(tmp_path)
    before = connection.execute(
        "SELECT status,decision,resolved_at,revision FROM conflict_cases WHERE id=?",
        (terminal_case_id,),
    ).fetchone()

    duplicate = _store(connection, "8080", "event-8080-again")

    assert duplicate.claim_id == winner_id
    assert duplicate.reason == "exact_duplicate"
    assert connection.execute("SELECT count(*) FROM conflict_cases").fetchone()[0] == 1
    assert (
        connection.execute(
            "SELECT count(*) FROM evidence_links WHERE derived_id=? AND evidence_type='event'",
            (winner_id,),
        ).fetchone()[0]
        == 2
    )
    after = connection.execute(
        "SELECT status,decision,resolved_at,revision FROM conflict_cases WHERE id=?",
        (terminal_case_id,),
    ).fetchone()
    assert tuple(after) == tuple(before)


def test_different_current_value_after_terminal_opens_next_generation(tmp_path) -> None:
    connection, winner_id, terminal_case_id = _resolved_generation_one(tmp_path)
    terminal_before = connection.execute(
        "SELECT status,decision,resolved_at,revision FROM conflict_cases WHERE id=?",
        (terminal_case_id,),
    ).fetchone()

    changed = _store(connection, "9090", "event-9090", state_change=True)

    assert changed.claim_id is not None and changed.claim_id != winner_id
    terminal_after = connection.execute(
        "SELECT status,decision,resolved_at,revision FROM conflict_cases WHERE id=?",
        (terminal_case_id,),
    ).fetchone()
    assert tuple(terminal_after) == tuple(terminal_before)
    cases = connection.execute(
        "SELECT id,generation,status,resolved_at FROM conflict_cases ORDER BY generation"
    ).fetchall()
    assert [(row["generation"], row["status"], row["resolved_at"]) for row in cases] == [
        (1, "resolved", NOW),
        (2, "manual_required", None),
    ]
    assert cases[1]["id"] != terminal_case_id
    assert (
        connection.execute(
            "SELECT count(*) FROM conflict_cases WHERE group_key IS NOT NULL "
            "AND status IN ('pending','auto_resolved','manual_required') AND resolved_at IS NULL"
        ).fetchone()[0]
        == 1
    )
    active_group = ClaimRepository(connection).find_by_conflict_key(
        ClaimRepository(connection).get_claim(winner_id)["conflict_key"]
    )
    assert {claim["status"] for claim in active_group} == {"disputed"}
    assert {claim["value"] for claim in active_group} == {"8080", "9090"}
