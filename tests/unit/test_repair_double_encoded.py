"""Tests for the frozen v0.19 two-row Claim repair."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.storage.migrations.fact_hash_v2 import compute_fact_hash_v2
from scripts.repair_v019_double_encoded_values import (
    APPROVED_CLAIM_IDS,
    DEFAULT_MANIFEST_PATH,
    load_manifest,
    main,
    repair_database,
)


def _manifest_items() -> list[dict[str, str]]:
    payload = json.loads(DEFAULT_MANIFEST_PATH.read_text(encoding="utf-8"))
    return payload["repairs"]


def _create_database(path: Path) -> None:
    database = Database(path)
    connection = database.open()
    repository = ClaimRepository(connection)
    for index, item in enumerate(_manifest_items(), start=1):
        value = json.loads(item["current_value_json"])
        subject = f"service-{index}"
        predicate = "description"
        repository.insert_claim(
            {
                "id": item["claim_id"],
                "namespace_key": "default",
                "subject_entity_id": subject,
                "predicate": predicate,
                "value": value,
                "qualifiers": {"project": "hl_mem"},
                "fact_hash": compute_fact_hash_v2(subject, predicate, value),
                "conflict_key": f"conflict-{index}",
                "conflict_key_version": 3,
                "legacy_conflict_key": f"legacy-{index}",
                "canonical_attribute": "fact.other",
                "canonical_slot": None,
                "topic_tags_json": '["memory"]',
                "index_text": f"old projection {index}",
                "embedding_dense": bytes([index]) * 16,
                "embedding_model": "fake",
                "embedding_dim": 4,
                "scope": "temporal",
                "importance": 0.8,
                "status": "active",
                "valid_from": "2026-01-01T00:00:00+00:00",
                "valid_to": None,
                "recorded_from": "2026-01-02T00:00:00+00:00",
                "recorded_to": None,
                "observed_at": "2026-01-01T00:00:00+00:00",
                "expires_at": "2026-08-01T00:00:00+00:00",
            }
        )
        connection.execute(
            "INSERT INTO evidence_links("
            "id,derived_type,derived_id,evidence_type,evidence_id,relation,weight"
            ") VALUES (?,?,?,?,?,?,?)",
            (
                f"evidence-{index}",
                "claim",
                item["claim_id"],
                "event",
                f"event-{index}",
                "supports",
                1.0,
            ),
        )
        connection.commit()

    repository.insert_claim(
        {
            "id": "canary-outside-manifest",
            "namespace_key": "default",
            "subject_entity_id": "canary",
            "predicate": "description",
            "value": "must remain untouched",
            "fact_hash": compute_fact_hash_v2(
                "canary",
                "description",
                "must remain untouched",
            ),
            "index_text": "canary projection",
            "status": "active",
            "recorded_from": "2026-01-02T00:00:00+00:00",
        }
    )
    database.close()


def _database_state(path: Path) -> tuple[dict[str, dict[str, Any]], list[tuple[Any, ...]]]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        claims = {str(row["id"]): dict(row) for row in connection.execute("SELECT * FROM claims ORDER BY id")}
        evidence = [tuple(row) for row in connection.execute("SELECT * FROM evidence_links ORDER BY id")]
        return claims, evidence
    finally:
        connection.close()


def test_repair_defaults_to_zero_write_dry_run_and_writes_audit_report(
    tmp_path: Path,
    capsys: Any,
) -> None:
    database_path = tmp_path / "repair dry run.db"
    report_path = tmp_path / "reports" / "repair.json"
    _create_database(database_path)
    before = _database_state(database_path)

    exit_code = main(
        [
            "--database",
            str(database_path),
            "--report-path",
            str(report_path),
        ]
    )
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert captured.err == ""
    assert exit_code == 0
    assert report["ok"] is True
    assert report["mode"] == "dry-run"
    assert report["checked"] == 2
    assert report["rows_found"] == 2
    assert report["would_apply"] == 2
    assert report["applied"] == 0
    assert report["outside_target_value_writes"] == 0
    assert all(item["hash_check"]["matches"] for item in report["repairs"])
    assert all(item["before"]["value_json"] for item in report["repairs"])
    assert all(item["target"]["value_json"] for item in report["repairs"])
    assert json.loads(report_path.read_text(encoding="utf-8")) == report
    assert _database_state(database_path) == before


def test_apply_changes_only_approved_values_and_fact_hashes(tmp_path: Path) -> None:
    database_path = tmp_path / "apply.db"
    _create_database(database_path)
    before_claims, before_evidence = _database_state(database_path)

    report = repair_database(database_path, apply=True)

    after_claims, after_evidence = _database_state(database_path)
    assert report["ok"] is True
    assert report["applied"] == 2
    assert report["checked"] == 2
    assert report["invariants_unchanged"] is True
    assert report["outside_target_value_writes"] == 0
    assert report["batch_before"]["protected_target_claims"] == report["batch_after"]["protected_target_claims"]
    assert report["batch_before"]["evidence"] == report["batch_after"]["evidence"]
    assert report["batch_before"]["canonical_claims"] != report["batch_after"]["canonical_claims"]

    manifest_by_id = {item["claim_id"]: item for item in _manifest_items()}
    for claim_id in APPROVED_CLAIM_IDS:
        before = before_claims[claim_id]
        after = after_claims[claim_id]
        changed_columns = {column for column in before if before[column] != after[column]}
        assert changed_columns == {"value_json", "fact_hash"}
        target_value_json = manifest_by_id[claim_id]["target_value_json"]
        assert after["value_json"] == target_value_json
        assert after["fact_hash"] == compute_fact_hash_v2(
            after["subject_entity_id"],
            after["predicate"],
            json.loads(target_value_json),
        )
        assert after["conflict_key"] == before["conflict_key"]
        assert after["conflict_key_version"] == before["conflict_key_version"]
        assert after["legacy_conflict_key"] == before["legacy_conflict_key"]

    assert after_claims["canary-outside-manifest"] == before_claims["canary-outside-manifest"]
    assert after_evidence == before_evidence
    assert all(item["conflict_key_recomputed"] is False for item in report["repairs"])
    assert all(item["after"] is not None for item in report["repairs"])


def test_one_before_hash_mismatch_rolls_back_the_whole_batch(tmp_path: Path) -> None:
    database_path = tmp_path / "cas-mismatch.db"
    _create_database(database_path)
    drifted_id = sorted(APPROVED_CLAIM_IDS)[1]
    connection = sqlite3.connect(database_path)
    connection.execute(
        "UPDATE claims SET value_json=? WHERE id=?",
        (json.dumps("drifted outside the frozen manifest"), drifted_id),
    )
    connection.commit()
    connection.close()
    before = _database_state(database_path)

    report = repair_database(database_path, apply=True)

    assert report["ok"] is False
    assert report["rolled_back"] is True
    assert report["applied"] == 0
    assert report["failure"]["type"] == "RuntimeError"
    assert "before-value hash mismatch" in report["failure"]["message"]
    assert _database_state(database_path) == before


def test_manifest_cannot_expand_the_approved_write_scope(tmp_path: Path) -> None:
    payload = json.loads(DEFAULT_MANIFEST_PATH.read_text(encoding="utf-8"))
    extra = dict(payload["repairs"][0])
    extra["claim_id"] = "unapproved-claim"
    payload["repairs"].append(extra)
    manifest_path = tmp_path / "expanded-manifest.json"
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    try:
        load_manifest(manifest_path)
    except ValueError as error:
        assert "unapproved manifest claim_id" in str(error)
    else:
        raise AssertionError("expanded repair manifest was accepted")


def test_target_fact_hash_collision_aborts_before_mutation(tmp_path: Path) -> None:
    database_path = tmp_path / "collision.db"
    _create_database(database_path)
    first = _manifest_items()[0]
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    target = connection.execute(
        "SELECT namespace_key,subject_entity_id,predicate FROM claims WHERE id=?",
        (first["claim_id"],),
    ).fetchone()
    target_fact_hash = compute_fact_hash_v2(
        target["subject_entity_id"],
        target["predicate"],
        json.loads(first["target_value_json"]),
    )
    connection.execute(
        "INSERT INTO claims("
        "id,namespace_key,subject_entity_id,predicate,value_json,fact_hash,"
        "recorded_from,status"
        ") VALUES (?,?,?,?,?,?,?,?)",
        (
            "collision-canary",
            target["namespace_key"],
            "other subject",
            "other predicate",
            json.dumps("other value"),
            target_fact_hash,
            "2026-01-02T00:00:00+00:00",
            "archived",
        ),
    )
    connection.commit()
    connection.close()
    before = _database_state(database_path)

    report = repair_database(database_path, apply=True)

    assert report["ok"] is False
    assert report["rolled_back"] is True
    assert report["applied"] == 0
    assert "target fact_hash collision" in report["failure"]["message"]
    assert _database_state(database_path) == before


def test_apply_rejects_unsafe_report_path_before_database_mutation(
    tmp_path: Path,
    capsys: Any,
) -> None:
    database_path = tmp_path / "unsafe-report.db"
    _create_database(database_path)
    before = _database_state(database_path)

    exit_code = main(
        [
            "--database",
            str(database_path),
            "--apply",
            "--report-path",
            f"{database_path}-shm",
        ]
    )
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert captured.err == ""
    assert exit_code == 1
    assert report["ok"] is False
    assert report["applied"] == 0
    assert report["failure"]["type"] == "ValueError"
    assert _database_state(database_path) == before
