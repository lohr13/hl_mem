from __future__ import annotations

import json
from dataclasses import replace

import pytest

from hl_mem import cli as cli_module
from hl_mem.application.ingest import IngestService
from hl_mem.application.model_coordinate_repair import (
    apply_model_coordinate_history_repair,
    inspect_model_coordinate_history,
)
from hl_mem.application.runtime_config_report import report_extraction_runtime
from hl_mem.errors import ConflictError
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.settings import Settings
from hl_mem.storage.database import Database
from hl_mem.storage.events import EventRepository

OLD = "2026-08-11T00:00:00+00:00"
FUTURE = "2099-01-01T00:00:00+00:00"


def _settings(path) -> Settings:
    return replace(
        Settings.for_test(),
        database_path=str(path),
        extractor_mode="llm",
        llm_provider="zhipu",
        llm_model="glm-5.3-flash",
        llm_base_url="https://example.invalid/v1",
        embedding_dim=8,
    )


def _seed_uncoordinated(
    connection,
    *,
    claim_id: str,
    subject: str,
    statement: str,
    recorded_at: str = OLD,
    with_evidence: bool = True,
) -> str:
    event = {
        "id": f"event-{claim_id}",
        "tenant_id": "default",
        "event_type": "conversation",
        "actor_type": "user",
        "content": {"text": statement},
        "occurred_at": recorded_at,
        "recorded_at": recorded_at,
        "origin_class": "direct_user",
        "session_kind": "interactive",
    }
    EventRepository(connection).insert_event(event)
    result = IngestService.store_extracted(
        connection,
        ExtractedClaim(
            predicate="使用",
            value=statement,
            subject=subject,
            qualifiers={},
            canonical_attribute="choice.model",
            canonical_slot=None,
            assertion_kind="observation",
        ),
        event,
        recorded_at,
        FakeEmbedder(8),
        authority="high",
    )
    assert result.claim_id is not None
    if not with_evidence:
        connection.execute("DELETE FROM evidence_links WHERE derived_id=?", (result.claim_id,))
    connection.commit()
    return result.claim_id


def _seed_history(connection) -> tuple[str, str, str, str, str]:
    extraction_id = _seed_uncoordinated(
        connection,
        claim_id="old-extraction",
        subject="HL-Mem extraction model",
        statement="HL-Mem extraction model currently uses qwen3.7-plus",
    )
    answering_id = _seed_uncoordinated(
        connection,
        claim_id="old-answering",
        subject="HL-Mem answering model",
        statement="HL-Mem answering model currently uses qwen-answering",
    )
    missing_evidence_id = _seed_uncoordinated(
        connection,
        claim_id="missing-evidence",
        subject="HL-Mem extraction model",
        statement="HL-Mem extraction model currently uses qwen-no-source",
        with_evidence=False,
    )
    future_id = _seed_uncoordinated(
        connection,
        claim_id="future-extraction",
        subject="HL-Mem extraction model",
        statement="HL-Mem extraction model currently uses future-model",
        recorded_at=FUTURE,
    )
    future_valid_id = _seed_uncoordinated(
        connection,
        claim_id="future-valid-extraction",
        subject="HL-Mem extraction model",
        statement="HL-Mem extraction model currently uses future-valid-model",
    )
    connection.execute(
        "UPDATE claims SET valid_from=?,observed_at=? WHERE id=?",
        (FUTURE, FUTURE, future_valid_id),
    )
    connection.commit()
    return extraction_id, answering_id, missing_evidence_id, future_id, future_valid_id


def test_history_inspection_is_read_only_and_selects_only_proven_older_extraction(tmp_path) -> None:
    settings = _settings(tmp_path / "inspect.db")
    database = Database(settings=settings)
    connection = database.open()
    extraction_id, answering_id, missing_evidence_id, future_id, future_valid_id = _seed_history(connection)
    winner = report_extraction_runtime(connection, settings)
    before_changes = connection.total_changes

    preview = inspect_model_coordinate_history(connection)

    assert connection.total_changes == before_changes
    assert preview["status"] == "ready"
    assert preview["dry_run"] is True
    assert preview["winner_claim_id"] == winner.claim_id
    assert preview["candidate_claim_count"] == 1
    assert preview["candidate_claim_ids"] == [extraction_id]
    assert preview["excluded_claim_ids"] == sorted([answering_id, missing_evidence_id, future_id, future_valid_id])
    database.close()


def test_history_repair_is_count_guarded_transactional_and_idempotent(tmp_path) -> None:
    settings = _settings(tmp_path / "apply.db")
    database = Database(settings=settings)
    connection = database.open()
    extraction_id, answering_id, _, _, _ = _seed_history(connection)
    winner = report_extraction_runtime(connection, settings)

    with pytest.raises(ConflictError, match="count mismatch"):
        apply_model_coordinate_history_repair(connection, expected_count=2)

    assert connection.execute("SELECT status FROM claims WHERE id=?", (extraction_id,)).fetchone()[0] == "active"

    result = apply_model_coordinate_history_repair(connection, expected_count=1)

    assert result["dry_run"] is False
    assert result["applied_claim_count"] == 1
    row = connection.execute(
        "SELECT status,superseded_by_id FROM claims WHERE id=?",
        (extraction_id,),
    ).fetchone()
    assert tuple(row) == ("superseded", winner.claim_id)
    assert connection.execute("SELECT status FROM claims WHERE id=?", (answering_id,)).fetchone()[0] == "active"
    assert (
        connection.execute(
            "SELECT count(*) FROM audit_log WHERE phase='claim_mutation' AND claim_id=?",
            (extraction_id,),
        ).fetchone()[0]
        >= 1
    )
    second_preview = inspect_model_coordinate_history(connection)
    assert second_preview["candidate_claim_count"] == 0
    database.close()


def test_history_repair_fails_closed_without_one_authoritative_winner(tmp_path) -> None:
    database = Database(tmp_path / "no-winner.db")
    connection = database.open()
    _seed_uncoordinated(
        connection,
        claim_id="old-extraction",
        subject="HL-Mem extraction model",
        statement="HL-Mem extraction model currently uses qwen3.7-plus",
    )

    preview = inspect_model_coordinate_history(connection)

    assert preview["status"] == "blocked"
    assert preview["blocker"] == "authoritative_winner_count:0"
    with pytest.raises(ConflictError, match="authoritative winner"):
        apply_model_coordinate_history_repair(connection, expected_count=0)
    database.close()


def test_coordinate_repair_cli_defaults_to_read_only_and_requires_count_for_apply(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    settings = _settings(tmp_path / "cli.db")
    database = Database(settings=settings)
    connection = database.open()
    extraction_id = _seed_uncoordinated(
        connection,
        claim_id="old-extraction",
        subject="HL-Mem extraction model",
        statement="HL-Mem extraction model currently uses qwen3.7-plus",
    )
    winner = report_extraction_runtime(connection, settings)
    database.close()
    before = (tmp_path / "cli.db").read_bytes()
    monkeypatch.setattr(cli_module, "load_settings", lambda *_: settings)

    cli_module.main(["coordinates", "repair-model-history", "--db", settings.database_path])

    raw_preview = capsys.readouterr().out
    preview = json.loads(raw_preview)
    assert preview["dry_run"] is True
    assert preview["candidate_claim_count"] == 1
    assert (tmp_path / "cli.db").read_bytes() == before
    assert "qwen3.7-plus" not in raw_preview

    with pytest.raises(ConflictError, match="expected-count"):
        cli_module.main(["coordinates", "repair-model-history", "--db", settings.database_path, "--apply"])

    cli_module.main(
        [
            "coordinates",
            "repair-model-history",
            "--db",
            settings.database_path,
            "--apply",
            "--expected-count",
            "1",
        ]
    )

    applied = json.loads(capsys.readouterr().out)
    assert applied["applied_claim_count"] == 1
    check = Database(settings=settings)
    row = (
        check.open()
        .execute(
            "SELECT status,superseded_by_id FROM claims WHERE id=?",
            (extraction_id,),
        )
        .fetchone()
    )
    assert tuple(row) == ("superseded", winner.claim_id)
    check.close()
