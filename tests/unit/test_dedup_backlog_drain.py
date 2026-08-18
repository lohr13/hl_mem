from __future__ import annotations

import json
from pathlib import Path

import pytest

import hl_mem.cli as cli_module
from hl_mem.application.dedup_backlog import (
    BELOW_FLOOR_REASON,
    drain_below_floor_pairs,
    inspect_below_floor_pairs,
)
from hl_mem.cli import main
from hl_mem.errors import ConflictError
from hl_mem.settings import Settings
from hl_mem.storage.database import Database

NOW = "2026-08-18T08:00:00+00:00"


def _fixture(path: Path, below_count: int = 597):
    connection = Database(path).open()
    connection.executemany(
        "INSERT INTO claims(id,value_json,recorded_from,status) VALUES (?,'\"x\"',?,'active')",
        ((claim_id, NOW) for claim_id in ("left", "right")),
    )
    rows = [
        (
            f"below-{index:03d}",
            f"below-pair-{index:03d}",
            "left",
            "right",
            "default" if index % 2 == 0 else "tenant-b",
            0.88 + (index % 40) / 1000,
            "ingest" if index % 3 == 0 else "legacy",
            NOW,
        )
        for index in range(below_count)
    ]
    rows.extend(
        (
            pair_id,
            pair_key,
            "left",
            "right",
            "default",
            similarity,
            "legacy",
            NOW,
        )
        for pair_id, pair_key, similarity in (
            ("at-floor", "at-floor-pair", 0.92),
            ("above-floor", "above-floor-pair", 0.96),
        )
    )
    connection.executemany(
        "INSERT INTO dedup_pairs("
        "id,pair_key,left_claim_id,right_claim_id,namespace_key,similarity,pair_source,created_at"
        ") VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    connection.execute(
        "INSERT INTO dedup_pairs("
        "id,pair_key,left_claim_id,right_claim_id,similarity,decision,judge_reason,reviewed_at,created_at"
        ") VALUES ('already-terminal','already-terminal-pair','left','right',0.90,'distinct','prior',?,?)",
        (NOW, NOW),
    )
    connection.commit()
    return connection


def test_dedup_backlog_dry_run_reports_597_without_mutation(tmp_path: Path) -> None:
    connection = _fixture(tmp_path / "dry-run.db")
    before_changes = connection.total_changes
    before_rows = [tuple(row) for row in connection.execute("SELECT id,decision FROM dedup_pairs ORDER BY id")]

    report = inspect_below_floor_pairs(connection, threshold=0.92)

    assert report["candidate_pair_count"] == 597
    assert report["threshold"] == 0.92
    assert report["terminal_decision"] == "dismissed_below_floor"
    assert report["judge_reason"] == BELOW_FLOOR_REASON
    assert report["pair_source_counts"] == {"ingest": 199, "legacy": 398}
    assert report["namespace_counts"] == {"default": 299, "tenant-b": 298}
    assert report["sample_pair_ids"] == [f"below-{index:03d}" for index in range(20)]
    assert report["sample_truncated"] is True
    assert connection.total_changes == before_changes
    assert [tuple(row) for row in connection.execute("SELECT id,decision FROM dedup_pairs ORDER BY id")] == before_rows


def test_dedup_backlog_expected_count_mismatch_rolls_back(tmp_path: Path) -> None:
    connection = _fixture(tmp_path / "mismatch.db", below_count=3)

    with pytest.raises(ConflictError, match="expected 597.*found 3"):
        drain_below_floor_pairs(connection, threshold=0.92, expected_count=597, reviewed_at=NOW)

    assert connection.execute("SELECT count(*) FROM dedup_pairs WHERE decision IS NULL").fetchone()[0] == 5
    assert connection.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 0


def test_dedup_backlog_drains_597_without_touching_claims_or_boundary_pairs(tmp_path: Path) -> None:
    connection = _fixture(tmp_path / "apply.db")
    claims_before = [tuple(row) for row in connection.execute("SELECT * FROM claims ORDER BY id")]

    report = drain_below_floor_pairs(
        connection,
        threshold=0.92,
        expected_count=597,
        reviewed_at=NOW,
        source="test-copy",
    )

    assert report["dry_run"] is False
    assert report["applied_pair_count"] == 597
    assert report["remaining_below_floor_count"] == 0
    assert report["claim_rows_updated"] == 0
    terminal = connection.execute(
        "SELECT count(*) FROM dedup_pairs WHERE decision='dismissed_below_floor' "
        "AND judge_reason=? AND policy_version='v2' AND reviewed_at=?",
        (BELOW_FLOOR_REASON, NOW),
    ).fetchone()[0]
    assert terminal == 597
    assert connection.execute("SELECT decision FROM dedup_pairs WHERE id='at-floor'").fetchone()[0] is None
    assert connection.execute("SELECT decision FROM dedup_pairs WHERE id='above-floor'").fetchone()[0] is None
    assert (
        connection.execute("SELECT decision FROM dedup_pairs WHERE id='already-terminal'").fetchone()[0] == "distinct"
    )
    assert [tuple(row) for row in connection.execute("SELECT * FROM claims ORDER BY id")] == claims_before
    audit = json.loads(
        connection.execute("SELECT detail_json FROM audit_log WHERE action='drain_dedup_below_floor'").fetchone()[0]
    )
    assert audit == {
        "applied_pair_count": 597,
        "item": "drain_dedup_below_floor",
        "judge_reason": BELOW_FLOOR_REASON,
        "source": "test-copy",
        "threshold": 0.92,
    }


def test_dedup_backlog_cli_is_readonly_by_default_and_apply_requires_count(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "cli.db"
    connection = _fixture(path, below_count=2)
    monkeypatch.setattr(cli_module, "load_settings", lambda *_: Settings.for_test())
    before = connection.total_changes

    main(["--db", str(path), "dedup", "drain-below-floor"])
    preview = json.loads(capsys.readouterr().out)

    assert preview["dry_run"] is True
    assert preview["candidate_pair_count"] == 2
    assert connection.total_changes == before
    with pytest.raises(ConflictError, match="expected-count is required"):
        main(["--db", str(path), "dedup", "drain-below-floor", "--apply"])

    main(
        [
            "--db",
            str(path),
            "dedup",
            "drain-below-floor",
            "--apply",
            "--expected-count",
            "2",
        ]
    )
    applied = json.loads(capsys.readouterr().out)
    assert applied["applied_pair_count"] == 2
    assert (
        connection.execute("SELECT count(*) FROM dedup_pairs WHERE decision='dismissed_below_floor'").fetchone()[0] == 2
    )
