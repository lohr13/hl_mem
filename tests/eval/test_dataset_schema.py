"""评测数据集校验与动态绑定测试。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tests.eval.dataset import BindingError, bind_cases, load_cases


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE claims (
            id TEXT PRIMARY KEY, subject_entity_id TEXT, predicate TEXT, value_json TEXT,
            qualifiers_json TEXT, status TEXT, confidence REAL, valid_from TEXT, valid_to TEXT,
            recorded_from TEXT, recorded_to TEXT
        );
        CREATE TABLE events (id TEXT PRIMARY KEY, content_json TEXT);
        CREATE TABLE evidence_links (
            derived_type TEXT, derived_id TEXT, evidence_type TEXT, evidence_id TEXT
        );
        """
    )
    return connection


def _write_case(path: Path, **overrides: object) -> None:
    case = {
        "id": "P01",
        "query": "默认数据库是什么？",
        "intent": "current_state",
        "expected_type": "claim",
        "expected_min_confidence": 0.9,
        "expected_status_filter": "active",
        "expected_keywords": ["SQLite", "WAL"],
        "keyword_match": "all",
        "binding": {"claim_keywords": ["SQLite", "WAL"], "evidence_keywords": ["数据库"]},
        "forbidden_statuses": ["superseded", "expired", "disputed"],
    }
    case.update(overrides)
    path.write_text(json.dumps(case, ensure_ascii=False) + "\n", encoding="utf-8")


def test_load_cases_validates_and_normalizes_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    _write_case(path)

    case = load_cases(path)[0]

    assert case.case_id == "P01"
    assert case.binding.claim_keywords == ("SQLite", "WAL")
    assert case.forbidden_statuses == ("superseded", "expired", "disputed")


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"id": ""}, "id"),
        ({"keyword_match": "sometimes"}, "keyword_match"),
        ({"expected_type": "empty", "expected_keywords": ["x"]}, "empty"),
        ({"binding": {}}, "claim_keywords"),
    ],
)
def test_load_cases_rejects_invalid_rows(tmp_path: Path, overrides: dict[str, object], message: str) -> None:
    path = tmp_path / "cases.jsonl"
    _write_case(path, **overrides)

    with pytest.raises(ValueError, match=message):
        load_cases(path)


def test_bind_cases_resolves_unique_claim_and_event_by_keywords(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    _write_case(path)
    connection = _connection()
    connection.execute(
        "INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "claim-1", "hl_mem", "配置", json.dumps("SQLite WAL"), "{}", "active", 0.95,
            "2026-01-01T00:00:00+00:00", None, "2026-01-01T00:00:00+00:00", None,
        ),
    )
    connection.execute("INSERT INTO events VALUES (?,?)", ("event-1", json.dumps({"text": "数据库使用 SQLite WAL"})))
    connection.execute("INSERT INTO evidence_links VALUES (?,?,?,?)", ("claim", "claim-1", "event", "event-1"))

    bound = bind_cases(connection, load_cases(path))[0]

    assert bound.relevant_claim_ids == ("claim-1",)
    assert bound.expected_evidence_event_ids == ("event-1",)


def test_bind_cases_reports_missing_and_accepts_multiple_relevant_claims(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    _write_case(path, binding={"claim_keywords": ["SQLite", "WAL"]})
    connection = _connection()
    cases = load_cases(path)

    with pytest.raises(BindingError, match="P01.*未找到"):
        bind_cases(connection, cases)

    for claim_id in ("one", "two"):
        connection.execute(
            "INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (claim_id, "hl_mem", "配置", '"SQLite WAL"', "{}", "active", 1.0, None, None, None, None),
        )
    assert bind_cases(connection, cases)[0].relevant_claim_ids == ("one", "two")


def test_empty_case_does_not_require_binding(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    _write_case(
        path,
        expected_type="empty",
        expected_min_confidence=None,
        expected_keywords=[],
        binding=None,
    )

    bound = bind_cases(_connection(), load_cases(path))[0]

    assert bound.relevant_claim_ids == ()


def test_recall_v2_dataset_contains_declared_50_cases() -> None:
    path = Path(__file__).parent / "datasets" / "recall_v2.jsonl"

    cases = load_cases(path)

    assert len(cases) == 50
    assert len({case.case_id for case in cases}) == 50
