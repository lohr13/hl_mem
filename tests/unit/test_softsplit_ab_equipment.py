from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from hl_mem.core.vector import pack_vector
from hl_mem.ingest.chunking import ChunkingPolicy
from hl_mem.ingest.llm_extractor import LLMExtractor
from hl_mem.llm.types import LLMResponse

EQUIPMENT_DIR = Path(__file__).parents[2] / "var/eval/softsplit_ab_20260827"


def _load_module(name: str) -> ModuleType:
    path = EQUIPMENT_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"softsplit_ab_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _create_corpus_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE audit_log(
            id INTEGER PRIMARY KEY,
            occurred_at TEXT NOT NULL,
            phase TEXT NOT NULL,
            action TEXT NOT NULL,
            outcome TEXT NOT NULL,
            trace_id TEXT NOT NULL,
            event_id TEXT,
            detail_json TEXT NOT NULL
        );
        CREATE TABLE events(
            id TEXT PRIMARY KEY,
            tenant_id TEXT,
            session_id TEXT,
            event_type TEXT,
            actor_type TEXT,
            content_json TEXT NOT NULL,
            content_hash TEXT,
            occurred_at TEXT,
            recorded_at TEXT,
            metadata_json TEXT
        );
        """)
    events = [
        ("event-a", '{"text":"secret alpha"}'),
        ("event-a2", '{"text":"secret alpha tail"}'),
        ("event-b", '{"text":"secret beta"}'),
        ("event-old", '{"text":"secret old"}'),
    ]
    connection.executemany(
        "INSERT INTO events(id,tenant_id,session_id,event_type,actor_type,content_json,occurred_at,recorded_at) "
        "VALUES(?, 'default', 'session', 'message', 'user', ?, '2026-08-20T00:00:00Z', "
        "'2026-08-20T00:00:01Z')",
        events,
    )
    rows = [
        (
            1,
            "2026-08-20T00:00:00Z",
            "extract",
            "possible_under_extraction",
            "claim_limit_reached",
            "event-a",
            "event-a",
            "{}",
        ),
        (
            2,
            "2026-08-20T00:00:01Z",
            "extraction",
            "evaluated",
            "claims",
            "event-a",
            "event-a",
            json.dumps({"source_event_ids": ["event-a", "event-a2"]}),
        ),
        (
            3,
            "2026-08-21T00:00:00Z",
            "extract",
            "possible_under_extraction",
            "claim_limit_reached",
            "event-a",
            "event-a",
            "{}",
        ),
        (
            4,
            "2026-08-22T00:00:00Z",
            "extract",
            "possible_under_extraction",
            "claim_limit_reached",
            "event-b",
            "event-b",
            "{}",
        ),
        (
            5,
            "2026-08-22T00:00:01Z",
            "extraction",
            "evaluated",
            "claims",
            "event-b",
            "event-b",
            json.dumps({"source_event_ids": ["event-b"]}),
        ),
        (
            6,
            "2026-08-22T00:00:02Z",
            "extract",
            "possible_under_extraction",
            "observed",
            "event-empty",
            "event-b",
            "{}",
        ),
        (
            7,
            "2026-08-18T23:59:59Z",
            "extract",
            "possible_under_extraction",
            "claim_limit_reached",
            "event-old",
            "event-old",
            "{}",
        ),
    ]
    connection.executemany(
        "INSERT INTO audit_log(id,occurred_at,phase,action,outcome,trace_id,event_id,detail_json) "
        "VALUES(?,?,?,?,?,?,?,?)",
        rows,
    )
    connection.commit()
    connection.close()


def test_export_manifest_locks_cases_without_copying_content(tmp_path: Path) -> None:
    exporter = _load_module("export_corpus")
    database_path = tmp_path / "production.db"
    output_path = tmp_path / "manifest.json"
    _create_corpus_database(database_path)

    manifest = exporter.export_manifest(
        database_path,
        output_path,
        since="2026-08-19T00:00:00Z",
        expected_cases=2,
        exported_at="2026-08-27T12:00:00Z",
    )

    assert manifest["case_count"] == 2
    assert [case["case_id"] for case in manifest["cases"]] == ["event-a", "event-b"]
    assert manifest["cases"][0]["source_event_ids"] == ["event-a", "event-a2"]
    assert all("content_sha256" in source for case in manifest["cases"] for source in case["sources"])
    serialized = output_path.read_text(encoding="utf-8")
    assert "secret alpha" not in serialized
    assert "content_json" not in serialized


def test_export_manifest_preserves_missing_source_as_unavailable(tmp_path: Path) -> None:
    exporter = _load_module("export_corpus")
    database_path = tmp_path / "production.db"
    output_path = tmp_path / "manifest.json"
    _create_corpus_database(database_path)
    connection = sqlite3.connect(database_path)
    connection.execute(
        "INSERT INTO audit_log(id,occurred_at,phase,action,outcome,trace_id,event_id,detail_json) "
        "VALUES(8, '2026-08-23T00:00:00Z', 'extract', 'possible_under_extraction', "
        "'claim_limit_reached', 'event-missing', 'event-missing', '{}')"
    )
    connection.commit()
    connection.close()

    manifest = exporter.export_manifest(
        database_path,
        output_path,
        since="2026-08-19T00:00:00Z",
        expected_cases=3,
        exported_at="2026-08-27T12:00:00Z",
    )

    missing = next(case for case in manifest["cases"] if case["case_id"] == "event-missing")
    assert missing["unavailable_source_event_ids"] == ["event-missing"]
    assert missing["sources"] == [
        {
            "event_id": "event-missing",
            "available": False,
            "content_sha256": None,
            "stored_content_hash": None,
        }
    ]


class _Provider:
    name = "fake"


class _SequenceClient:
    model = "qwen3.7-plus"
    provider = _Provider()

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[Any] = []

    def complete(self, request):
        self.requests.append(request)
        return self.responses.pop(0)


class _ConstantEmbedder:
    model = "constant-test"
    dim = 2

    def embed_batch(self, texts: list[str]) -> list[bytes]:
        return [pack_vector([1.0, 0.0]) for _ in texts]


def _compact_claim(value: str) -> dict[str, object]:
    return {
        "subject": "user",
        "value": value,
        "kind": "fact",
        "confidence": 1.0,
        "notability": "high",
        "evidence_quote": value,
        "source_event_indices": [0],
    }


def _response(values: list[str]) -> LLMResponse:
    return LLMResponse(
        json.dumps({"claims": [_compact_claim(value) for value in values], "should_memorize": True}),
        "stop",
        10,
    )


def test_runner_replays_control_root_and_records_three_treatment_requests() -> None:
    runner = _load_module("run_ab")
    left = [f"left fact {index:02d}" for index in range(20)]
    right = [f"right fact {index:02d}" for index in range(20)]
    root = left[:10] + right[:10]
    clients = iter(
        [
            _SequenceClient([_response(root)]),
            _SequenceClient([_response(left), _response(right)]),
        ]
    )

    def extractor_factory(client, soft_split_enabled: bool):
        return LLMExtractor(
            client,
            ChunkingPolicy(12_000, 0, 3),
            soft_split_enabled=soft_split_enabled,
        )

    record = runner.run_extraction_pair(
        "case-1",
        "\n\n".join(left + right),
        {"occurred_at": "2026-08-20T00:00:00Z"},
        client_factory=lambda: next(clients),
        extractor_factory=extractor_factory,
        embedder=_ConstantEmbedder(),
    )

    assert record["status"] == "success"
    assert len(record["control"]["requests"]) == 1
    assert len(record["treatment"]["requests"]) == 3
    assert record["treatment"]["requests"][0]["cache_hit"] is True
    assert record["comparison"]["net_new_after_split"] == 20
    assert record["treatment"]["request_summary"] == {
        "expected_count": 3,
        "observed_count": 3,
        "failed_count": 0,
        "failed_or_missing_count": 0,
    }


def _score_record(index: int, *, net_new: int, duplicate_delta_pp: float, failed: int = 0) -> dict[str, Any]:
    control_claims = 100
    control_duplicates = 5
    treatment_claims = 100
    treatment_duplicates = round(control_duplicates + duplicate_delta_pp)
    return {
        "case_id": f"case-{index:02d}",
        "status": "failed" if failed else "success",
        "control": {
            "duplicate_profile": {
                "claim_count": control_claims,
                "duplicate_count": control_duplicates,
            }
        },
        "treatment": {
            "duplicate_profile": {
                "claim_count": treatment_claims,
                "duplicate_count": treatment_duplicates,
            },
            "request_summary": {
                "expected_count": 3,
                "observed_count": 3,
                "failed_count": failed,
                "failed_or_missing_count": failed,
            },
        },
        "comparison": {"net_new_after_split": net_new},
    }


def test_scorer_applies_all_three_frozen_gates() -> None:
    scorer = _load_module("score_results")
    passing = [
        _score_record(index, net_new=4 if index < 18 else 3, duplicate_delta_pp=4, failed=0) for index in range(34)
    ]
    passing[0]["treatment"]["request_summary"]["failed_count"] = 2
    passing[0]["treatment"]["request_summary"]["failed_or_missing_count"] = 2

    report = scorer.score_records(passing, expected_case_count=34)

    assert report["overall"] == "PASS"
    assert report["gates"]["effective_output"]["status"] == "PASS"
    assert report["gates"]["duplicate_pollution"]["delta_pp"] == 4.0
    assert report["gates"]["request_failure"]["failure_rate"] < 0.02

    failing = [
        _score_record(index, net_new=0, duplicate_delta_pp=6, failed=3 if index == 0 else 0) for index in range(34)
    ]
    failing_report = scorer.score_records(failing, expected_case_count=34)

    assert failing_report["overall"] == "FAIL"
    assert failing_report["gates"]["duplicate_pollution"]["status"] == "FAIL"
    assert failing_report["gates"]["request_failure"]["status"] == "FAIL"
