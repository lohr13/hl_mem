"""Claim search projection and embedding integrity tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from hl_mem.domain.claims.claim import build_index_text
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.workers.index_integrity import check_index_integrity


def _insert_claim(
    connection,
    *,
    claim_id: str,
    status: str,
    embedder: FakeEmbedder,
) -> None:
    text = "hl_mem uses SQLite choice.database database"
    ClaimRepository(connection).insert_claim(
        {
            "id": claim_id,
            "namespace_key": "default",
            "subject_entity_id": "hl_mem",
            "predicate": "uses",
            "value": "SQLite",
            "canonical_slot": "choice.database",
            "topic_tags_json": '["database"]',
            "index_text": text,
            "embedding_dense": embedder.embed_one(text),
            "embedding_model": embedder.model,
            "embedding_dim": embedder.dim,
            "status": status,
            "recorded_from": "2026-07-31T00:00:00+00:00",
        }
    )


def test_index_integrity_checks_recallable_statuses_without_writes(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "integrity.db")
    connection = database.open()
    embedder = FakeEmbedder(8)
    for status in ("active", "superseded", "expired", "candidate"):
        _insert_claim(
            connection,
            claim_id=f"claim-{status}",
            status=status,
            embedder=embedder,
        )
    before = connection.total_changes

    report = check_index_integrity(
        connection,
        mode="legacy",
        expected_model=embedder.model,
        expected_dim=embedder.dim,
    )

    assert report.ok
    assert report.checked == 3
    assert report.statuses == ("active", "superseded", "expired")
    assert report.fts_expected_rows == 3
    assert report.fts_covered_rows == 3
    assert connection.total_changes == before
    database.close()


def test_index_integrity_reports_projection_embedding_and_metadata_failures(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "failures.db")
    connection = database.open()
    embedder = FakeEmbedder(8)
    _insert_claim(
        connection,
        claim_id="projection",
        status="active",
        embedder=embedder,
    )
    _insert_claim(
        connection,
        claim_id="missing",
        status="superseded",
        embedder=embedder,
    )
    _insert_claim(
        connection,
        claim_id="length",
        status="expired",
        embedder=embedder,
    )
    connection.execute("UPDATE claims SET index_text='stale projection' WHERE id='projection'")
    connection.execute("UPDATE claims SET embedding_dense=NULL WHERE id='missing'")
    connection.execute(
        "UPDATE claims SET embedding_dense=?,embedding_model='old',embedding_dim=4 " "WHERE id='length'",
        (b"\x00" * 8,),
    )
    connection.commit()

    report = check_index_integrity(
        connection,
        mode="legacy",
        expected_model=embedder.model,
        expected_dim=embedder.dim,
    )

    assert not report.ok
    assert report.projection_mismatches == 1
    assert report.missing_embeddings == 1
    assert report.embedding_length_mismatches == 1
    assert report.model_dim_mismatches == 1
    assert report.sample_ids == {
        "embedding_length_mismatches": ("length",),
        "missing_embeddings": ("missing",),
        "model_dim_mismatches": ("length",),
        "projection_mismatches": ("projection",),
    }
    database.close()


def test_index_integrity_projects_answerable_required_qualifiers(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "answerable.db")
    connection = database.open()
    embedder = FakeEmbedder(8)
    claim = {
        "subject_entity_id": "hl_mem",
        "predicate": "uses",
        "value": "SQLite",
        "qualifiers": {"project": "v019"},
        "canonical_slot": "choice.database",
        "topic_tags": ["database"],
    }
    text = build_index_text(claim, mode="answerable")
    ClaimRepository(connection).insert_claim(
        {
            "id": "claim-1",
            "namespace_key": "default",
            "subject_entity_id": claim["subject_entity_id"],
            "predicate": claim["predicate"],
            "value": claim["value"],
            "qualifiers": claim["qualifiers"],
            "canonical_slot": claim["canonical_slot"],
            "topic_tags_json": '["database"]',
            "index_text": text,
            "embedding_dense": embedder.embed_one(text),
            "embedding_model": embedder.model,
            "embedding_dim": embedder.dim,
            "status": "active",
            "recorded_from": "2026-07-31T00:00:00+00:00",
        }
    )

    report = check_index_integrity(
        connection,
        mode="answerable",
        expected_model=embedder.model,
        expected_dim=embedder.dim,
    )

    assert report.ok
    database.close()


def test_index_integrity_detects_fts_external_content_mismatch(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "fts.db")
    connection = database.open()
    embedder = FakeEmbedder(8)
    _insert_claim(
        connection,
        claim_id="claim-1",
        status="active",
        embedder=embedder,
    )
    connection.execute("DROP TRIGGER claims_au")
    connection.execute("UPDATE claims SET index_text='content no longer matches FTS' WHERE id='claim-1'")
    connection.commit()
    before = connection.total_changes

    report = check_index_integrity(
        connection,
        mode="legacy",
        expected_model=embedder.model,
        expected_dim=embedder.dim,
    )

    assert not report.ok
    assert not report.fts_integrity_ok
    assert report.fts_error is not None
    assert connection.total_changes == before
    database.close()


def test_index_integrity_detects_missing_fts_row(tmp_path: Path) -> None:
    database = Database(tmp_path / "fts-coverage.db")
    connection = database.open()
    embedder = FakeEmbedder(8)
    _insert_claim(
        connection,
        claim_id="claim-1",
        status="active",
        embedder=embedder,
    )
    rowid = connection.execute("SELECT rowid FROM claims WHERE id='claim-1'").fetchone()[0]
    connection.execute(
        "DELETE FROM claims_fts_docsize WHERE id=?",
        (rowid,),
    )
    connection.commit()

    report = check_index_integrity(
        connection,
        mode="legacy",
        expected_model=embedder.model,
        expected_dim=embedder.dim,
    )

    assert not report.ok
    assert report.fts_expected_rows == 1
    assert report.fts_covered_rows == 0
    assert report.fts_missing_rows == 1
    assert report.sample_ids["fts_missing_rows"] == ("claim-1",)
    database.close()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"expected_model": "", "expected_dim": 8}, "expected_model"),
        ({"expected_model": "fake", "expected_dim": 0}, "expected_dim"),
    ],
)
def test_index_integrity_rejects_invalid_expectations(
    tmp_path: Path,
    kwargs: dict[str, object],
    message: str,
) -> None:
    database = Database(tmp_path / "invalid.db")
    connection = database.open()
    with pytest.raises(ValueError, match=message):
        check_index_integrity(connection, mode="legacy", **kwargs)
    database.close()
