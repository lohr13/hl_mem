import json

import pytest

from hl_mem import __version__
from hl_mem.cli import export_database, import_database, list_conflicts, main, resolve_conflict
from hl_mem.mcp.server import McpMemoryServer
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database


def test_mcp_exposes_minimal_memory_tool_contract(tmp_path) -> None:
    server = McpMemoryServer(tmp_path / "mcp.db")
    assert set(server.list_tools()) == {
        "memory_recall",
        "memory_save",
        "memory_get",
        "memory_correct",
        "memory_forget",
        "memory_explain",
        "memory_feedback",
    }
    saved = server.call_tool("memory_save", {"text": "记住 SQLite", "subject": "项目"})
    assert saved["id"]
    assert server.call_tool("memory_explain", {"id": saved["id"]})["type"] == "event"


def test_cli_export_import_round_trip(tmp_path) -> None:
    source = tmp_path / "source.db"
    connection = Database(source).open()
    connection.execute(
        "INSERT INTO events(id,event_type,actor_type,content_json,occurred_at,recorded_at) VALUES (?,?,?,?,?,?)",
        (
            "e1",
            "message",
            "user",
            json.dumps({"text": "中文"}, ensure_ascii=False),
            "2026-01-01",
            "2026-01-01",
        ),
    )
    connection.commit()
    archive = tmp_path / "memory.jsonl"
    assert export_database(source, archive) == 1
    target = tmp_path / "target.db"
    report = import_database(target, archive)
    assert report == {
        "processed": 1,
        "events_created": 1,
        "events_skipped": 0,
        "jobs_queued": 1,
        "failed_batch": None,
        "claims_not_rebuilt": False,
    }
    target_connection = Database(target).open()
    assert target_connection.execute("SELECT content_json FROM events WHERE id='e1'").fetchone()[0]
    assert (
        target_connection.execute("SELECT idempotency_key FROM jobs WHERE job_type='extract_event'").fetchone()[0]
        == "extract:e1"
    )


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit, match="0"):
        main(["--version"])

    assert f"hl_mem {__version__}" in capsys.readouterr().out


def _conflict_claim(
    repository: ClaimRepository,
    claim_id: str,
    value: object,
    *,
    status: str,
    source_authority: str,
    recorded_from: str,
) -> None:
    assert repository.insert_claim(
        {
            "id": claim_id,
            "namespace_key": "default",
            "subject_entity_id": "用户",
            "predicate": "使用",
            "value": value,
            "status": status,
            "scope": "permanent",
            "source_authority": source_authority,
            "recorded_from": recorded_from,
        }
    )


def _manual_conflict(repository: ClaimRepository, case_id: str = "case") -> None:
    assert repository.insert_conflict_case(
        {
            "id": case_id,
            "pair_key": f"left:right:{case_id}",
            "left_claim_id": "left",
            "right_claim_id": "right",
            "status": "manual_required",
            "created_at": "2026-01-03T00:00:00+00:00",
        }
    )


def test_cli_keep_left_supersedes_loser_atomically(tmp_path) -> None:
    path = tmp_path / "resolve-keep-left.db"
    connection = Database(path).open()
    repository = ClaimRepository(connection)
    _conflict_claim(
        repository,
        "left",
        "SQLite",
        status="disputed",
        source_authority="high",
        recorded_from="2026-01-01T00:00:00+00:00",
    )
    _conflict_claim(
        repository,
        "right",
        "PostgreSQL",
        status="disputed",
        source_authority="low",
        recorded_from="2026-01-02T00:00:00+00:00",
    )
    _manual_conflict(repository)

    result = resolve_conflict(path, "case", "keep_left")

    assert repository.get_claim("left")["status"] == "active"
    loser = repository.get_claim("right")
    assert (loser["status"], loser["superseded_by_id"], loser["valid_to"], loser["recorded_to"]) == (
        "superseded",
        "left",
        result["resolved_at"],
        result["resolved_at"],
    )
    assert tuple(
        connection.execute("SELECT status,decision,resolved_at FROM conflict_cases WHERE id='case'").fetchone()
    ) == ("resolved", "keep_left", result["resolved_at"])


def test_cli_keep_left_does_not_mutate_terminal_loser(tmp_path) -> None:
    path = tmp_path / "resolve-terminal-loser.db"
    connection = Database(path).open()
    repository = ClaimRepository(connection)
    _conflict_claim(
        repository,
        "left",
        "SQLite",
        status="disputed",
        source_authority="high",
        recorded_from="2026-01-01T00:00:00+00:00",
    )
    _conflict_claim(
        repository,
        "right",
        "PostgreSQL",
        status="expired",
        source_authority="low",
        recorded_from="2026-01-02T00:00:00+00:00",
    )
    _manual_conflict(repository)

    resolve_conflict(path, "case", "keep_left")

    loser = repository.get_claim("right")
    assert (loser["status"], loser["superseded_by_id"], loser["valid_to"], loser["recorded_to"]) == (
        "expired",
        None,
        None,
        None,
    )


def test_cli_list_conflicts_includes_claim_context_and_bounded_values(tmp_path) -> None:
    path = tmp_path / "list-conflicts.db"
    connection = Database(path).open()
    repository = ClaimRepository(connection)
    _conflict_claim(
        repository,
        "left",
        "左" * 200,
        status="disputed",
        source_authority="high",
        recorded_from="2026-01-01T00:00:00+00:00",
    )
    _conflict_claim(
        repository,
        "right",
        {"choice": "right"},
        status="candidate",
        source_authority="low",
        recorded_from="2026-01-02T00:00:00+00:00",
    )
    _manual_conflict(repository)

    [item] = list_conflicts(path)

    assert item["left_value"] == "左" * 157 + "..."
    assert item["right_value"] == '{"choice": "right"}'
    assert len(item["left_value"]) == 160
    assert {
        "left_status": item["left_status"],
        "right_status": item["right_status"],
        "left_authority": item["left_authority"],
        "right_authority": item["right_authority"],
        "left_recorded_from": item["left_recorded_from"],
        "right_recorded_from": item["right_recorded_from"],
    } == {
        "left_status": "disputed",
        "right_status": "candidate",
        "left_authority": "high",
        "right_authority": "low",
        "left_recorded_from": "2026-01-01T00:00:00+00:00",
        "right_recorded_from": "2026-01-02T00:00:00+00:00",
    }
