from __future__ import annotations

import json
from pathlib import Path

import pytest

import hl_mem.cli as cli_module
from hl_mem.application.expired_cleanup import (
    cleanup_expired_claims,
    inspect_expired_claims,
    maintain_expired_claims,
)
from hl_mem.cli import main
from hl_mem.errors import ConflictError
from hl_mem.settings import Settings
from hl_mem.storage.database import Database
from hl_mem.storage.tombstones import TombstoneLedger

NOW = "2026-08-18T08:00:00+00:00"
OLD = "2026-04-01T00:00:00+00:00"
RECENT = "2026-08-01T00:00:00+00:00"


def _claim(connection, claim_id: str, status: str, valid_to: str | None) -> None:
    connection.execute(
        "INSERT INTO claims(id,value_json,recorded_from,status,valid_to) VALUES (?, '\"x\"', ?, ?, ?)",
        (claim_id, OLD, status, valid_to),
    )


def _fixture(path: Path):
    connection = Database(path).open()
    for claim_id, status, valid_to in (
        ("eligible-a", "expired", OLD),
        ("eligible-b", "expired", OLD),
        ("consumer-blocked", "expired", OLD),
        ("conflict-blocked", "expired", OLD),
        ("too-recent", "expired", RECENT),
        ("active-old", "active", OLD),
        ("consumer", "active", None),
        ("conflict-other", "active", None),
    ):
        _claim(connection, claim_id, status, valid_to)
    connection.execute(
        "INSERT INTO evidence_links(id,derived_type,derived_id,evidence_type,evidence_id,relation) "
        "VALUES ('consumer-link','claim','consumer','claim','consumer-blocked','derived_from')"
    )
    connection.execute(
        "INSERT INTO conflict_cases(id,pair_key,left_claim_id,right_claim_id,status,created_at) "
        "VALUES ('open-case','open-pair','conflict-blocked','conflict-other','manual_required',?)",
        (OLD,),
    )
    connection.commit()
    return connection


def test_expired_cleanup_dry_run_explains_eligibility_without_writes(tmp_path: Path) -> None:
    connection = _fixture(tmp_path / "inspect.db")
    before = [tuple(row) for row in connection.execute("SELECT * FROM claims ORDER BY id")]

    report = inspect_expired_claims(connection, now=NOW, retention_days=90)

    assert report == {
        "as_of": NOW,
        "retention_days": 90,
        "cutoff": "2026-05-20T08:00:00+00:00",
        "expired_claim_count": 5,
        "eligible_claim_count": 2,
        "too_recent_count": 1,
        "evidence_consumer_count": 1,
        "open_conflict_count": 1,
        "sample_eligible_claim_ids": ["eligible-a", "eligible-b"],
        "sample_truncated": False,
    }
    assert [tuple(row) for row in connection.execute("SELECT * FROM claims ORDER BY id")] == before
    assert not (tmp_path / "inspect.db.tombstones.db").exists()


def test_expired_cleanup_expected_count_mismatch_is_fail_closed(tmp_path: Path) -> None:
    database_path = tmp_path / "mismatch.db"
    connection = _fixture(database_path)

    with pytest.raises(ConflictError, match="expected 3.*found 2"):
        cleanup_expired_claims(
            connection,
            now=NOW,
            retention_days=90,
            batch_size=1,
            expected_count=3,
        )

    assert connection.execute("SELECT count(*) FROM claims").fetchone()[0] == 8
    assert not (tmp_path / "mismatch.db.tombstones.db").exists()


def test_expired_cleanup_is_bounded_and_uses_tombstone_deletion_closure(tmp_path: Path) -> None:
    database_path = tmp_path / "apply.db"
    connection = _fixture(database_path)

    first = cleanup_expired_claims(
        connection,
        now=NOW,
        retention_days=90,
        batch_size=1,
        expected_count=2,
        source="test-copy",
    )

    assert first["scanned"] == 1
    assert first["deleted"] == 1
    assert first["remaining_eligible_count"] == 1
    assert connection.execute("SELECT 1 FROM claims WHERE id='eligible-a'").fetchone() is None
    assert connection.execute("SELECT 1 FROM claims WHERE id='eligible-b'").fetchone() is not None
    assert connection.execute("SELECT 1 FROM claims WHERE id='consumer-blocked'").fetchone() is not None
    assert connection.execute("SELECT 1 FROM evidence_links WHERE id='consumer-link'").fetchone() is not None
    assert TombstoneLedger(tmp_path / "apply.db.tombstones.db", create=False).count() == 1

    second = cleanup_expired_claims(
        connection,
        now=NOW,
        retention_days=90,
        batch_size=10,
        expected_count=1,
        source="test-copy",
    )
    assert second["deleted"] == 1
    assert second["remaining_eligible_count"] == 0
    assert TombstoneLedger(tmp_path / "apply.db.tombstones.db", create=False).count() == 2


def test_expired_maintenance_defaults_to_observe_and_on_rechecks_expected_count(tmp_path: Path) -> None:
    connection = _fixture(tmp_path / "maintenance.db")

    observed = maintain_expired_claims(
        connection,
        now=NOW,
        retention_days=90,
        batch_size=1,
        mode="observe",
    )
    applied = maintain_expired_claims(
        connection,
        now=NOW,
        retention_days=90,
        batch_size=1,
        mode="on",
    )

    assert observed["dry_run"] is True
    assert observed["eligible_claim_count"] == 2
    assert applied["expected_count"] == 2
    assert applied["deleted"] == 1


def test_expired_cleanup_cli_defaults_to_readonly_report_and_apply_requires_count(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "cli.db"
    connection = _fixture(path)
    monkeypatch.setattr(cli_module, "load_settings", lambda *_: Settings.for_test())

    main(["--db", str(path), "expired", "cleanup", "--now", NOW])
    preview = json.loads(capsys.readouterr().out)

    assert preview["dry_run"] is True
    assert preview["eligible_claim_count"] == 2
    assert not (tmp_path / "cli.db.tombstones.db").exists()
    with pytest.raises(ConflictError, match="expected-count is required"):
        main(["--db", str(path), "expired", "cleanup", "--apply", "--now", NOW])

    main(
        [
            "--db",
            str(path),
            "expired",
            "cleanup",
            "--apply",
            "--expected-count",
            "2",
            "--limit",
            "1",
            "--now",
            NOW,
        ]
    )
    applied = json.loads(capsys.readouterr().out)
    assert applied["deleted"] == 1
    assert connection.execute("SELECT count(*) FROM claims WHERE status='expired'").fetchone()[0] == 4
