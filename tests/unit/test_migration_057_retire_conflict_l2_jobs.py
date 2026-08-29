from __future__ import annotations

import json
from pathlib import Path

from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database

MIGRATION = "057_retire_conflict_l2_jobs"
NOW = "2026-08-30T08:00:00+00:00"


def _claim(repository: ClaimRepository, claim_id: str, value: str) -> None:
    assert repository.insert_claim(
        {
            "id": claim_id,
            "namespace_key": "default",
            "subject_entity_id": "gateway",
            "predicate": "配置",
            "value": value,
            "qualifiers": {"service": "gateway"},
            "canonical_attribute": "config.port",
            "canonical_slot": "config.port",
            "fact_hash": f"hash-{claim_id}",
            "conflict_key": f"key-{claim_id}",
            "conflict_key_version": 3,
            "recorded_from": NOW,
            "status": "disputed",
            "source_authority": "medium",
            "scope": "permanent",
            "volatility": "stable",
        }
    )


def _job(
    connection,
    job_id: str,
    status: str,
    *,
    case_id: str = "case-open",
    job_type: str = "resolve_conflict_llm",
) -> None:
    connection.execute(
        "INSERT INTO jobs(id,job_type,payload_json,status,leased_until,lease_token,last_error,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            job_id,
            job_type,
            json.dumps({"case_id": case_id}),
            status,
            "2026-08-30T09:00:00+00:00",
            f"lease-{job_id}",
            f"error-{job_id}",
            NOW,
            NOW,
        ),
    )


def _seed_pre_057(path: Path) -> dict[str, tuple[object, ...]]:
    database = Database(path)
    connection = database.open()
    repository = ClaimRepository(connection)
    for claim_id, value in (
        ("open-left", "8080"),
        ("open-right", "8081"),
        ("closed-left", "9090"),
        ("closed-right", "9091"),
    ):
        _claim(repository, claim_id, value)
    connection.execute(
        "INSERT INTO conflict_cases(id,pair_key,left_claim_id,right_claim_id,status,decision,created_at) "
        "VALUES ('case-open','pair-open','open-left','open-right','manual_required','uncertain',?)",
        (NOW,),
    )
    connection.execute(
        "INSERT INTO conflict_cases(id,pair_key,left_claim_id,right_claim_id,status,decision,created_at,resolved_at) "
        "VALUES ('case-closed','pair-closed','closed-left','closed-right','resolved','keep_left',?,?)",
        (NOW, NOW),
    )
    connection.execute(
        "UPDATE conflict_review_state SET dirty_at=NULL,dirty_reason='pre_057_clean',"
        "not_before='2026-09-01T00:00:00+00:00',attempt_count=3,last_error='retry later' "
        "WHERE case_id='case-open'"
    )
    for status in ("pending", "running", "failed", "succeeded", "dead"):
        _job(connection, f"l2-{status}", status)
    _job(connection, "l2-closed", "pending", case_id="case-closed")
    _job(connection, "other-running", "running", job_type="extract_event")
    terminal_before = {
        row["id"]: tuple(row)
        for row in connection.execute(
            "SELECT id,status,leased_until,lease_token,last_error,updated_at FROM jobs "
            "WHERE id IN ('l2-succeeded','l2-dead','other-running') ORDER BY id"
        )
    }
    connection.execute("DELETE FROM schema_migrations WHERE version=?", (MIGRATION,))
    connection.commit()
    database.close()
    return terminal_before


def test_057_retires_only_nonterminal_l2_jobs_and_redirties_open_cases(tmp_path: Path) -> None:
    path = tmp_path / "retire-l2.db"
    terminal_before = _seed_pre_057(path)

    database = Database(path)
    connection = database.open()

    retired = {
        row["id"]: tuple(row)[1:]
        for row in connection.execute(
            "SELECT id,status,leased_until,lease_token FROM jobs "
            "WHERE id IN ('l2-pending','l2-running','l2-failed','l2-closed') ORDER BY id"
        )
    }
    assert retired == {
        "l2-closed": ("dead", None, None),
        "l2-failed": ("dead", None, None),
        "l2-pending": ("dead", None, None),
        "l2-running": ("dead", None, None),
    }
    terminal_after = {
        row["id"]: tuple(row)
        for row in connection.execute(
            "SELECT id,status,leased_until,lease_token,last_error,updated_at FROM jobs "
            "WHERE id IN ('l2-succeeded','l2-dead','other-running') ORDER BY id"
        )
    }
    assert terminal_after == terminal_before
    review = connection.execute(
        "SELECT dirty_at,dirty_reason,not_before,attempt_count,last_error "
        "FROM conflict_review_state WHERE case_id='case-open'"
    ).fetchone()
    assert review["dirty_at"] is not None
    assert tuple(review)[1:] == ("conflict_l2_retired", None, 0, None)
    assert connection.execute("SELECT 1 FROM conflict_review_state WHERE case_id='case-closed'").fetchone() is None
    assert connection.execute("SELECT count(*) FROM schema_migrations WHERE version=?", (MIGRATION,)).fetchone()[0] == 1

    connection.execute(
        "UPDATE conflict_review_state SET dirty_at=NULL,dirty_reason='post_057_clean' WHERE case_id='case-open'"
    )
    connection.execute("DELETE FROM schema_migrations WHERE version=?", (MIGRATION,))
    connection.commit()
    database.close()

    reopened = Database(path).open()
    assert tuple(
        reopened.execute("SELECT dirty_at,dirty_reason FROM conflict_review_state WHERE case_id='case-open'").fetchone()
    ) == (None, "post_057_clean")
