from __future__ import annotations

import json
from dataclasses import replace

import hl_mem.cli as cli_module
import hl_mem.workers.worker as worker_module
from hl_mem.cli import main
from hl_mem.monitoring.worker import WorkerRuntimeState
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.workers.worker import Worker

NOW = "2026-08-16T00:00:00+00:00"


def _claim(repository: ClaimRepository, claim_id: str, *, status: str = "disputed") -> None:
    assert repository.insert_claim(
        {
            "id": claim_id,
            "namespace_key": "default",
            "subject_entity_id": "gateway",
            "predicate": "uses",
            "value": claim_id,
            "status": status,
            "recorded_from": NOW,
        }
    )


def _dangling_case(
    connection,
    case_id: str,
    *,
    status: str,
    left_claim_id: str,
    right_claim_id: str,
) -> None:
    connection.execute(
        "INSERT INTO conflict_cases("
        "id,pair_key,left_claim_id,right_claim_id,status,decision,created_at,resolved_at"
        ") VALUES (?,?,?,?,?,?,?,?)",
        (
            case_id,
            f"pair:{case_id}",
            left_claim_id,
            right_claim_id,
            status,
            "reject" if status in {"resolved", "rejected"} else None,
            NOW,
            NOW if status in {"resolved", "rejected"} else None,
        ),
    )


def _seed_dangling_cases(connection) -> None:
    repository = ClaimRepository(connection)
    _claim(repository, "existing-left")
    connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")
    _dangling_case(
        connection,
        "terminal-both",
        status="resolved",
        left_claim_id="missing-left",
        right_claim_id="missing-right",
    )
    _dangling_case(
        connection,
        "terminal-one",
        status="rejected",
        left_claim_id="existing-left",
        right_claim_id="missing-terminal-right",
    )
    _dangling_case(
        connection,
        "open-both",
        status="manual_required",
        left_claim_id="missing-open-left",
        right_claim_id="missing-open-right",
    )
    connection.commit()
    connection.execute("PRAGMA foreign_keys=ON")


def test_worker_repairs_only_terminal_both_missing_with_limit_and_audit(tmp_path) -> None:
    path = tmp_path / "worker-repair.db"
    connection = Database(path).open()
    connection.execute("PRAGMA foreign_keys=OFF")
    for index in range(101):
        _dangling_case(
            connection,
            f"terminal-{index:03d}",
            status="resolved",
            left_claim_id=f"missing-left-{index}",
            right_claim_id=f"missing-right-{index}",
        )
    _dangling_case(
        connection,
        "open-both",
        status="pending",
        left_claim_id="missing-open-left",
        right_claim_id="missing-open-right",
    )
    connection.commit()
    connection.execute("PRAGMA foreign_keys=ON")
    worker = Worker(
        replace(Settings.for_test(), database_path=str(path)),
        connection=connection,
    )

    worker._run_maintenance()

    remaining_terminal = connection.execute(
        "SELECT id FROM conflict_cases WHERE status='resolved' ORDER BY id"
    ).fetchall()
    assert [row["id"] for row in remaining_terminal] == ["terminal-100"]
    assert connection.execute("SELECT status FROM conflict_cases WHERE id='open-both'").fetchone()[0] == "pending"
    audit = connection.execute(
        "SELECT phase,action,outcome,detail_json FROM audit_log "
        "WHERE json_extract(detail_json,'$.item')='repair_dangling_conflicts'"
    ).fetchone()
    assert tuple(audit[:3]) == ("worker", "maintenance", "success")
    detail = json.loads(audit["detail_json"])
    assert detail == {
        "case_ids": [f"terminal-{index:03d}" for index in range(20)],
        "case_ids_truncated": True,
        "deleted_count": 100,
        "item": "repair_dangling_conflicts",
        "source": "worker",
    }


def test_worker_repairs_dangling_before_auto_resolve_and_stops_failure_accumulation(tmp_path) -> None:
    path = tmp_path / "worker-order.db"
    connection = Database(path).open()
    repository = ClaimRepository(connection)
    _claim(repository, "left")
    _claim(repository, "right")
    assert repository.insert_conflict_case(
        {
            "id": "live-case",
            "pair_key": "left:right",
            "left_claim_id": "left",
            "right_claim_id": "right",
            "status": "pending",
            "created_at": NOW,
        }
    )
    connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")
    _dangling_case(
        connection,
        "terminal-both",
        status="resolved",
        left_claim_id="missing-left",
        right_claim_id="missing-right",
    )
    connection.commit()
    connection.execute("PRAGMA foreign_keys=ON")
    runtime = WorkerRuntimeState()
    worker = Worker(
        replace(Settings.for_test(), database_path=str(path), conflict_auto_mode="l0_only"),
        connection=connection,
        worker_runtime=runtime,
    )

    worker._run_maintenance_item(
        "auto_resolve_conflicts",
        lambda: worker_module.auto_resolve_conflicts(connection, NOW),
    )
    assert runtime.snapshot()["failure_counts"] == {"auto_resolve_conflicts": 1}

    worker._run_maintenance()

    assert runtime.snapshot()["failure_counts"] == {"auto_resolve_conflicts": 1}
    assert connection.execute("SELECT 1 FROM conflict_cases WHERE id='terminal-both'").fetchone() is None
    assert connection.execute("SELECT status FROM conflict_cases WHERE id='live-case'").fetchone()[0] == (
        "manual_required"
    )


def test_cli_repair_dangling_dry_run_lists_without_mutation(tmp_path, capsys, monkeypatch) -> None:
    path = tmp_path / "cli-dry-run.db"
    connection = Database(path).open()
    _seed_dangling_cases(connection)
    monkeypatch.setattr(cli_module, "load_settings", lambda *_: Settings.for_test())

    main(["--db", str(path), "conflicts", "repair-dangling"])

    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"] is True
    assert result["deleted_count"] == 0
    assert result["deleted_case_ids"] == []
    assert result["cases"] == [
        {
            "category": "open_dangling",
            "id": "open-both",
            "left_claim_id": "missing-open-left",
            "left_exists": False,
            "right_claim_id": "missing-open-right",
            "right_exists": False,
            "status": "manual_required",
            "suggested_action": "manual_review",
        },
        {
            "category": "terminal_both_missing",
            "id": "terminal-both",
            "left_claim_id": "missing-left",
            "left_exists": False,
            "right_claim_id": "missing-right",
            "right_exists": False,
            "status": "resolved",
            "suggested_action": "delete",
        },
        {
            "category": "terminal_one_side",
            "id": "terminal-one",
            "left_claim_id": "existing-left",
            "left_exists": True,
            "right_claim_id": "missing-terminal-right",
            "right_exists": False,
            "status": "rejected",
            "suggested_action": "manual_review",
        },
    ]
    assert connection.execute("SELECT count(*) FROM conflict_cases").fetchone()[0] == 3
    assert connection.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 0


def test_cli_repair_dangling_apply_only_deletes_terminal_both_missing(tmp_path, capsys, monkeypatch) -> None:
    path = tmp_path / "cli-apply.db"
    connection = Database(path).open()
    _seed_dangling_cases(connection)
    monkeypatch.setattr(cli_module, "load_settings", lambda *_: Settings.for_test())

    main(["--db", str(path), "conflicts", "repair-dangling", "--apply"])

    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"] is False
    assert result["deleted_count"] == 1
    assert result["deleted_case_ids"] == ["terminal-both"]
    rows = connection.execute("SELECT id FROM conflict_cases ORDER BY id").fetchall()
    assert [row["id"] for row in rows] == ["open-both", "terminal-one"]
    detail = json.loads(
        connection.execute(
            "SELECT detail_json FROM audit_log " "WHERE json_extract(detail_json,'$.item')='repair_dangling_conflicts'"
        ).fetchone()[0]
    )
    assert detail["source"] == "cli"
    assert detail["deleted_count"] == 1
    assert detail["case_ids"] == ["terminal-both"]


def test_cli_repair_dangling_reports_empty_when_no_references_are_dangling(tmp_path, capsys, monkeypatch) -> None:
    path = tmp_path / "cli-empty.db"
    Database(path).open().close()
    monkeypatch.setattr(cli_module, "load_settings", lambda *_: Settings.for_test())

    main(["--db", str(path), "conflicts", "repair-dangling"])

    assert json.loads(capsys.readouterr().out) == {
        "cases": [],
        "deleted_case_ids": [],
        "deleted_count": 0,
        "dry_run": True,
    }
