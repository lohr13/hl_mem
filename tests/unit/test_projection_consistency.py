"""Post-release Claim projection consistency verifier tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from hl_mem.domain.claims.claim import build_index_text
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from scripts.verify_projection_consistency import main


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _create_database(path: Path) -> FakeEmbedder:
    database = Database(path)
    connection = database.open()
    embedder = FakeEmbedder(8)
    for status in ("active", "superseded", "expired"):
        claim = {
            "subject_entity_id": "hl_mem",
            "predicate": "uses",
            "value": f"SQLite {status}",
            "qualifiers": {},
            "canonical_slot": "choice.database",
            "topic_tags": ["database"],
        }
        index_text = build_index_text(claim, mode="legacy")
        stored_claim = {key: value for key, value in claim.items() if key != "topic_tags"}
        ClaimRepository(connection).insert_claim(
            {
                "id": f"claim-{status}",
                "namespace_key": "default",
                **stored_claim,
                "topic_tags_json": '["database"]',
                "index_text": index_text,
                "embedding_dense": embedder.embed_one(index_text),
                "embedding_model": embedder.model,
                "embedding_dim": embedder.dim,
                "status": status,
                "recorded_from": "2026-07-31T00:00:00+00:00",
            }
        )

    ClaimRepository(connection).insert_claim(
        {
            "id": "claim-candidate",
            "namespace_key": "default",
            "subject_entity_id": "ignored",
            "predicate": "ignored",
            "value": "ignored",
            "index_text": "stale candidate projection",
            "embedding_dense": None,
            "embedding_model": "old-model",
            "embedding_dim": 1,
            "status": "candidate",
            "recorded_from": "2026-07-31T00:00:00+00:00",
        }
    )
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    database.close()
    return embedder


def _run(
    database_path: Path,
    capsys: pytest.CaptureFixture[str],
    *extra: str,
) -> tuple[int, dict[str, object]]:
    exit_code = main(
        [
            "--database",
            str(database_path),
            "--mode",
            "legacy",
            "--projection-version",
            "v019-legacy-v1",
            "--embedding-model",
            "fake",
            "--embedding-dim",
            "8",
            *extra,
        ]
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    return exit_code, json.loads(captured.out)


def test_projection_verifier_succeeds_and_never_writes_source(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "clean.db"
    report_path = tmp_path / "reports" / "projection.json"
    _create_database(database_path)
    before = _file_sha256(database_path)

    exit_code, report = _run(
        database_path,
        capsys,
        "--report-path",
        str(report_path),
    )

    assert exit_code == 0
    assert report["ok"] is True
    assert report["recallable_statuses"] == ["active", "superseded", "expired"]
    assert report["expected_projection_version"] == "v019-legacy-v1"
    assert report["projection_version_source"] == "explicit_arguments"
    assert report["projection_version_persisted_per_claim"] is False
    assert report["deployment_configuration_verified"] is False
    assert report["projection"]["uniform_current_projection"] is True
    assert report["projection_inconsistency_detected"] is False
    assert report["mixed_projection_detected"] is False
    assert report["integrity"]["checked"] == 3
    assert report["embedding"]["model_dim_distribution"] == [{"claim_count": 3, "dim": 8, "model": "fake"}]
    assert report["embedding"]["model_dim_matches_expected"] is True
    assert json.loads(report_path.read_text(encoding="utf-8")) == report
    assert _file_sha256(database_path) == before


def test_projection_verifier_rejects_mixed_current_and_old_projections(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "mixed.db"
    _create_database(database_path)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT subject_entity_id,predicate,value_json,qualifiers_json,"
        "canonical_slot,topic_tags_json FROM claims WHERE id='claim-active'"
    ).fetchone()
    answerable = build_index_text(
        {
            "subject_entity_id": row["subject_entity_id"],
            "predicate": row["predicate"],
            "value": json.loads(row["value_json"]),
            "qualifiers": json.loads(row["qualifiers_json"]),
            "canonical_slot": row["canonical_slot"],
            "topic_tags": json.loads(row["topic_tags_json"]),
        },
        mode="answerable",
    )
    connection.execute(
        "UPDATE claims SET index_text=?,embedding_dense=? WHERE id='claim-active'",
        (answerable, FakeEmbedder(8).embed_one(answerable)),
    )
    connection.commit()
    connection.close()

    exit_code = main(
        [
            "--database",
            str(database_path),
            "--mode",
            "answerable",
            "--projection-version",
            "v019-answerable-v1",
            "--embedding-model",
            "fake",
            "--embedding-dim",
            "8",
        ]
    )
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert captured.err == ""
    assert exit_code == 1
    assert report["ok"] is False
    assert report["projection"]["uniform_current_projection"] is False
    assert report["projection_inconsistency_detected"] is True
    assert report["mixed_projection_detected"] is None
    assert report["integrity"]["projection_mismatches"] == 2


def test_projection_verifier_rejects_embedding_distribution_and_length(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "embedding.db"
    _create_database(database_path)
    connection = sqlite3.connect(database_path)
    connection.execute(
        "UPDATE claims SET embedding_dense=?,embedding_model='old-model' " "WHERE id='claim-active'",
        (b"\x00" * 4,),
    )
    connection.commit()
    connection.close()

    exit_code, report = _run(database_path, capsys)

    assert exit_code == 1
    assert report["ok"] is False
    assert report["embedding"]["single_model_dim"] is False
    assert report["embedding"]["model_dim_matches_expected"] is False
    assert report["integrity"]["embedding_length_mismatches"] == 1
    assert report["integrity"]["model_dim_mismatches"] == 1


def test_projection_verifier_rejects_fts_external_content_mismatch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "fts.db"
    _create_database(database_path)
    connection = sqlite3.connect(database_path)
    connection.execute("DROP TRIGGER claims_au")
    connection.execute("UPDATE claims SET index_text='content not present in FTS' " "WHERE id='claim-active'")
    connection.commit()
    connection.close()

    exit_code, report = _run(database_path, capsys)

    assert exit_code == 1
    assert report["ok"] is False
    assert report["integrity"]["fts_integrity_ok"] is False
    assert report["integrity"]["fts_error"]


def test_projection_verifier_reports_schema_errors_as_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "empty-schema.db"
    sqlite3.connect(database_path).close()

    exit_code, report = _run(database_path, capsys)

    assert exit_code == 1
    assert report["ok"] is False
    assert report["projection_version_source"] == "explicit_arguments"
    assert report["mixed_projection_detected"] is None
    assert report["error"]["type"] == "OperationalError"


def test_projection_verifier_refuses_database_sidecar_as_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "sidecar.db"
    _create_database(database_path)
    before = _file_sha256(database_path)

    exit_code, report = _run(
        database_path,
        capsys,
        "--report-path",
        f"{database_path}-wal",
    )

    assert exit_code == 1
    assert report["ok"] is False
    assert report["report_error"]["type"] == "ValueError"
    assert _file_sha256(database_path) == before
