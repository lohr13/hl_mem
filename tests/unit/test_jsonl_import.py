from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import hl_mem.cli as cli_module
from hl_mem.cli import JSONLImportError, import_database
from hl_mem.settings import Settings
from hl_mem.storage.jobs import JobRepository
from hl_mem.workers.worker import Worker


def _event(event_id: str, text: str | None = None) -> dict[str, object]:
    return {
        "id": event_id,
        "tenant_id": "default",
        "event_type": "message",
        "actor_type": "user",
        "content_json": json.dumps({"text": text or event_id}, ensure_ascii=False),
        "occurred_at": "2026-01-01T00:00:00Z",
        "recorded_at": "2026-01-01T00:00:00Z",
    }


def _write_archive(path: Path, events: list[dict[str, object]]) -> None:
    records = [{"type": "metadata", "format_version": "1"}]
    records.extend({"type": "event", "data": event} for event in events)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def _row_counts(path: Path) -> tuple[int, int]:
    connection = sqlite3.connect(path)
    try:
        return (
            connection.execute("SELECT count(*) FROM events").fetchone()[0],
            connection.execute("SELECT count(*) FROM jobs WHERE job_type='extract_event'").fetchone()[0],
        )
    finally:
        connection.close()


def test_import_queues_one_stable_job_per_new_event_and_is_idempotent(tmp_path: Path) -> None:
    archive = tmp_path / "events.jsonl"
    target = tmp_path / "target.db"
    _write_archive(archive, [_event("event-1"), _event("event-2")])

    first = import_database(target, archive)
    second = import_database(target, archive)

    assert first == {
        "processed": 2,
        "events_created": 2,
        "events_skipped": 0,
        "jobs_queued": 2,
        "failed_batch": None,
        "claims_not_rebuilt": False,
    }
    assert second == {
        "processed": 2,
        "events_created": 0,
        "events_skipped": 2,
        "jobs_queued": 0,
        "failed_batch": None,
        "claims_not_rebuilt": False,
    }
    assert _row_counts(target) == (2, 2)
    connection = sqlite3.connect(target)
    try:
        assert {
            row[0] for row in connection.execute("SELECT idempotency_key FROM jobs WHERE job_type='extract_event'")
        } == {"extract:event-1", "extract:event-2"}
    finally:
        connection.close()


def test_import_rolls_back_event_when_extraction_job_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "events.jsonl"
    target = tmp_path / "target.db"
    _write_archive(archive, [_event("event-1")])

    def fail_job(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        raise sqlite3.OperationalError("injected job failure")

    monkeypatch.setattr(JobRepository, "insert_job", fail_job)
    with pytest.raises(JSONLImportError) as captured:
        import_database(target, archive)

    assert captured.value.report["processed"] == 0
    assert captured.value.report["failed_batch"] == {
        "batch": 1,
        "line": 2,
        "error": "injected job failure",
    }
    assert _row_counts(target) == (0, 0)


def test_invalid_record_rolls_back_current_batch_and_reports_line(tmp_path: Path) -> None:
    archive = tmp_path / "events.jsonl"
    target = tmp_path / "target.db"
    _write_archive(archive, [_event("event-1"), {**_event("event-2"), "id": ""}])

    with pytest.raises(JSONLImportError) as captured:
        import_database(target, archive, batch_size=10)

    assert captured.value.report["processed"] == 0
    assert captured.value.report["events_created"] == 0
    assert captured.value.report["jobs_queued"] == 0
    assert captured.value.report["failed_batch"]["batch"] == 1
    assert captured.value.report["failed_batch"]["line"] == 3
    assert _row_counts(target) == (0, 0)


def test_skip_extraction_jobs_marks_claims_not_rebuilt(tmp_path: Path) -> None:
    archive = tmp_path / "events.jsonl"
    target = tmp_path / "target.db"
    _write_archive(archive, [_event("event-1")])

    report = import_database(target, archive, skip_extraction_jobs=True)

    assert report["events_created"] == 1
    assert report["jobs_queued"] == 0
    assert report["claims_not_rebuilt"] is True
    assert _row_counts(target) == (1, 0)


def test_normal_reimport_backfills_jobs_after_forensic_skip(tmp_path: Path) -> None:
    archive = tmp_path / "events.jsonl"
    target = tmp_path / "target.db"
    _write_archive(archive, [_event("event-1")])
    import_database(target, archive, skip_extraction_jobs=True)

    report = import_database(target, archive)
    repeated = import_database(target, archive)

    assert report["events_created"] == 0
    assert report["events_skipped"] == 1
    assert report["jobs_queued"] == 1
    assert report["claims_not_rebuilt"] is False
    assert repeated["jobs_queued"] == 0
    assert _row_counts(target) == (1, 1)


def test_duplicate_event_id_with_different_payload_is_rejected(tmp_path: Path) -> None:
    original = tmp_path / "original.jsonl"
    conflicting = tmp_path / "conflicting.jsonl"
    target = tmp_path / "target.db"
    _write_archive(original, [_event("event-1", "原始内容")])
    _write_archive(conflicting, [_event("event-1", "冲突内容")])
    import_database(target, original, skip_extraction_jobs=True)

    with pytest.raises(JSONLImportError, match="existing event payload") as captured:
        import_database(target, conflicting)

    assert captured.value.report["processed"] == 0
    assert captured.value.report["jobs_queued"] == 0
    assert _row_counts(target) == (1, 0)


def test_worker_rebuilds_claim_from_imported_archive(tmp_path: Path) -> None:
    archive = tmp_path / "events.jsonl"
    target = tmp_path / "target.db"
    _write_archive(archive, [_event("event-1", "我喜欢 SQLite")])
    import_database(target, archive)
    worker = Worker(Settings(database_path=str(target)))
    try:
        result = worker.run_once()
        claim = worker.connection.execute("SELECT predicate,value_json,status FROM claims").fetchone()
    finally:
        worker.database.close()

    assert result["status"] == "succeeded"
    assert result["stored"] == 1
    assert tuple(claim) == ("偏好", json.dumps("SQLite", ensure_ascii=False), "active")


def test_import_cli_outputs_report_and_skip_flag(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "events.jsonl"
    target = tmp_path / "target.db"
    _write_archive(archive, [_event("event-1")])
    monkeypatch.setattr(cli_module, "load_settings", lambda *_: Settings())

    cli_module.main(
        [
            "import",
            str(archive),
            "--db",
            str(target),
            "--skip-extraction-jobs",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert output["processed"] == 1
    assert output["claims_not_rebuilt"] is True
    assert output["jobs_queued"] == 0
