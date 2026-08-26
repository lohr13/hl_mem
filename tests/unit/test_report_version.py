from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

import hl_mem
from hl_mem.application.version_report import report_version
from hl_mem.cli import main
from hl_mem.settings import Settings
from hl_mem.storage.database import Database


def test_report_version_cli_stores_fixed_event_and_claim_without_llm_job(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "probe.db"
    config = tmp_path / "hl_mem.toml"
    config.write_text(f'[database]\npath = "{database_path.as_posix()}"\n', encoding="utf-8")

    main(["--config", str(config), "report-version", "--namespace", "default", "--subject", "HL-Mem"])

    output = json.loads(capsys.readouterr().out)
    connection = Database(database_path).open()
    event = connection.execute(
        "SELECT event_type,actor_type,content_json FROM events WHERE id=?", (output["event_id"],)
    ).fetchone()
    claim = connection.execute(
        "SELECT claim.canonical_slot,claim.assertion_kind,claim.value_json,claim.subject_canonical_entity_id "
        "FROM claims claim JOIN evidence_links link ON link.derived_id=claim.id "
        "WHERE link.evidence_type='event' AND link.evidence_id=?",
        (output["event_id"],),
    ).fetchone()
    assert output == {
        "event_id": output["event_id"],
        "owner": "project:hl_mem",
        "producer_contract": "hl_mem.report-version-v1",
        "queued": False,
        "reported_version": hl_mem.__version__,
        "stored": True,
    }
    assert (event["event_type"], event["actor_type"]) == ("status_report", "tool")
    assert json.loads(event["content_json"])["producer_contract"] == "hl_mem.report-version-v1"
    assert tuple(claim) == ("config.version", "observation", json.dumps(hl_mem.__version__), "project:hl_mem")
    assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_report_version_requires_one_active_typed_owner(tmp_path: Path) -> None:
    connection = Database(tmp_path / "missing-owner.db").open()

    with pytest.raises(ValueError):
        report_version(connection, namespace="default", subject="not-a-known-owner")

    assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 0


def test_report_version_is_idempotent_per_version_and_rollback_creates_new_occurrence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = Database(
        tmp_path / "rollback.db", settings=replace(Settings.for_test(), latest_wins_mode="enforce")
    ).open()
    clock = iter(f"2026-08-26T0{hour}:00:00+00:00" for hour in range(1, 5))
    monkeypatch.setattr("hl_mem.application.version_report._now", lambda: next(clock))
    monkeypatch.setattr(hl_mem, "__version__", "0.31.1")
    first = report_version(connection, namespace="default", subject="HL-Mem")
    repeated = report_version(connection, namespace="default", subject="HL-Mem")
    monkeypatch.setattr(hl_mem, "__version__", "0.32.0")
    upgraded = report_version(connection, namespace="default", subject="HL-Mem")
    monkeypatch.setattr(hl_mem, "__version__", "0.31.1")
    rolled_back = report_version(connection, namespace="default", subject="HL-Mem")

    claim_ids = [
        connection.execute(
            "SELECT derived_id FROM evidence_links WHERE evidence_type='event' AND evidence_id=?",
            (report["event_id"],),
        ).fetchone()[0]
        for report in (first, repeated, upgraded, rolled_back)
    ]
    first_id, repeated_id, upgraded_id, rolled_back_id = claim_ids
    assert repeated_id == first_id
    assert upgraded_id != first_id
    assert rolled_back_id not in {first_id, upgraded_id}
    assert (
        connection.execute(
            "SELECT count(*) FROM evidence_links WHERE derived_type='claim' AND derived_id=?",
            (first_id,),
        ).fetchone()[0]
        == 2
    )
    active = connection.execute("SELECT id,value_json FROM claims WHERE status='active'").fetchall()
    assert [(row["id"], json.loads(row["value_json"])) for row in active] == [(rolled_back_id, "0.31.1")]


@pytest.mark.parametrize("forged", [("--version", "9.9.9"), ("--producer-contract", "fake-v1")])
def test_cli_exposes_no_free_version_or_producer_argument(tmp_path: Path, forged: tuple[str, str]) -> None:
    config = tmp_path / "hl_mem.toml"
    config.write_text('[database]\npath = "memory.db"\n', encoding="utf-8")

    with pytest.raises(SystemExit) as caught:
        main(["--config", str(config), "report-version", "--subject", "HL-Mem", *forged])

    assert caught.value.code == 2
