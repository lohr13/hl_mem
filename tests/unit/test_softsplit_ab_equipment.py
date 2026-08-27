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


def _request(
    status: str = "success",
    *,
    error_class: str | None = None,
    cache_hit: bool = False,
) -> dict[str, Any]:
    request: dict[str, Any] = {"status": status, "cache_hit": cache_hit}
    if error_class is not None:
        request["error"] = {"class": error_class, "message": "test error"}
    return request


def _score_record(index: int, *, net_new: int, duplicate_delta_pp: float, failed: int = 0) -> dict[str, Any]:
    control_claims = 100
    control_duplicates = 5
    treatment_claims = 100
    treatment_duplicates = round(control_duplicates + duplicate_delta_pp)
    treatment_requests = [
        _request(
            "error" if request_index < failed else "success",
            error_class="HTTPStatusError" if request_index < failed else None,
        )
        for request_index in range(3)
    ]
    return {
        "case_id": f"case-{index:02d}",
        "status": "failed" if failed else "success",
        "failure_reasons": ["treatment_error"] if failed else [],
        "control": {
            "error": None,
            "requests": [_request()],
            "duplicate_profile": {
                "claim_count": control_claims,
                "duplicate_count": control_duplicates,
            },
        },
        "treatment": {
            "error": {"class": "HTTPStatusError", "message": "rate limited"} if failed else None,
            "requests": treatment_requests,
            "audit_events": [],
            "duplicate_profile": {
                "claim_count": treatment_claims,
                "duplicate_count": treatment_duplicates,
            },
        },
        "comparison": {"net_new_after_split": net_new},
    }


def test_scorer_applies_all_v2_frozen_gates() -> None:
    scorer = _load_module("score_results")
    passing = [
        _score_record(index, net_new=4 if index < 18 else 3, duplicate_delta_pp=4, failed=0) for index in range(34)
    ]
    passing[0]["treatment"]["requests"][0] = _request("error", error_class="HTTPStatusError")
    passing[0]["treatment"]["requests"][1] = _request("error", error_class="ConnectError")

    report = scorer.score_records(passing, expected_case_count=34)

    assert report["overall"] == "PASS"
    assert report["gates"]["effective_output"]["status"] == "PASS"
    assert report["gates"]["duplicate_pollution"]["delta_pp"] == 4.0
    assert report["gates"]["transport_failure"]["failure_rate"] < 0.02
    assert report["gates"]["extraction_quality_failure"]["failure_rate"] == 0.0

    failing = [
        _score_record(index, net_new=0, duplicate_delta_pp=6, failed=3 if index == 0 else 0) for index in range(34)
    ]
    failing_report = scorer.score_records(failing, expected_case_count=34)

    assert failing_report["overall"] == "FAIL"
    assert failing_report["gates"]["duplicate_pollution"]["status"] == "FAIL"
    assert failing_report["gates"]["transport_failure"]["status"] == "FAIL"
    assert failing_report["gates"]["extraction_quality_failure"]["status"] == "PASS"


def test_scorer_classifies_failures_without_charging_unissued_drift_requests() -> None:
    scorer = _load_module("score_results")
    success = _score_record(0, net_new=4, duplicate_delta_pp=0)
    success["treatment"]["audit_events"] = [
        {"outcome": "claim_limit_residual_after_split"},
        {"outcome": "claim_limit_residual_after_split"},
    ]
    runner_error = {
        "case_id": "case-01",
        "status": "failed",
        "failure_reasons": ["runner_error"],
        "error": {"class": "ValueError", "message": "source event is missing"},
    }
    replay_drift = _score_record(2, net_new=0, duplicate_delta_pp=0)
    replay_drift.update(
        status="failed",
        failure_reasons=[
            "control_root_not_compact_exact_20",
            "treatment_soft_split_not_applied",
            "treatment_request_count_not_3",
        ],
    )
    replay_drift["treatment"]["requests"] = [_request()]
    api_error = _score_record(3, net_new=0, duplicate_delta_pp=0, failed=1)
    api_error["treatment"]["requests"] = [_request("error", error_class="HTTPStatusError")]
    protocol_deviation = _score_record(4, net_new=3, duplicate_delta_pp=0)
    protocol_deviation.update(status="failed", failure_reasons=["treatment_request_count_not_3"])
    protocol_deviation["treatment"]["requests"] = [_request() for _ in range(5)]

    report = scorer.score_records(
        [success, runner_error, replay_drift, api_error, protocol_deviation],
        expected_case_count=5,
    )

    assert report["classification"]["case_counts"] == {
        "success": 1,
        "runner_error": 1,
        "replay_drift": 1,
        "api_error": 1,
        "protocol_deviation": 1,
    }
    duplicate_gate = report["gates"]["duplicate_pollution"]
    assert duplicate_gate["status"] == "PASS"
    assert duplicate_gate["metrics_complete"] is True
    assert duplicate_gate["scored_case_count"] == 4
    assert duplicate_gate["excluded_runner_error_count"] == 1
    transport_gate = report["gates"]["transport_failure"]
    assert transport_gate["non_cache_treatment_request_count"] == 10
    assert transport_gate["transport_failure_count"] == 1
    assert transport_gate["failure_rate"] == 0.1
    quality_gate = report["gates"]["extraction_quality_failure"]
    assert quality_gate["extraction_failure_count"] == 0
    assert quality_gate["failure_rate"] == 0.0
    assert report["diagnostics"]["residual_saturation"] == {
        "successful_case_count": 1,
        "cases_with_residual": 1,
        "case_rate": 1.0,
        "residual_event_count": 2,
    }


def test_scorer_counts_schema_validation_as_one_treatment_failure_unit() -> None:
    scorer = _load_module("score_results")
    record = _score_record(0, net_new=0, duplicate_delta_pp=0)
    record.update(status="failed", failure_reasons=["treatment_error"])
    record["treatment"]["requests"] = [_request() for _ in range(4)]
    record["treatment"]["error"] = {
        "class": "LLMSchemaValidationError",
        "message": "claims remain over limit after auto split",
    }

    report = scorer.score_records([record], expected_case_count=1)

    assert report["classification"]["case_counts"]["api_error"] == 1
    transport_gate = report["gates"]["transport_failure"]
    assert transport_gate["non_cache_treatment_request_count"] == 4
    assert transport_gate["transport_failure_count"] == 0
    assert transport_gate["failure_rate"] == 0.0
    quality_gate = report["gates"]["extraction_quality_failure"]
    assert quality_gate["non_cache_treatment_request_count"] == 4
    assert quality_gate["extraction_failure_count"] == 1
    assert quality_gate["failure_rate"] == 0.25


def test_scorer_counts_mixed_transport_and_schema_failures_separately() -> None:
    scorer = _load_module("score_results")
    record = _score_record(0, net_new=4, duplicate_delta_pp=0)
    record.update(status="failed", failure_reasons=["treatment_error"])
    record["treatment"]["requests"] = [
        _request("success", cache_hit=True),
        _request("error", error_class="httpx.ConnectTimeout"),
        _request(),
    ]
    record["treatment"]["error"] = {
        "class": "LLMOutputTruncatedError",
        "message": "child response was truncated",
    }

    report = scorer.score_records([record], expected_case_count=1)

    transport_gate = report["gates"]["transport_failure"]
    assert transport_gate["non_cache_treatment_request_count"] == 2
    assert transport_gate["cached_treatment_request_count"] == 1
    assert transport_gate["transport_failure_count"] == 1
    assert transport_gate["failure_rate"] == 0.5
    quality_gate = report["gates"]["extraction_quality_failure"]
    assert quality_gate["non_cache_treatment_request_count"] == 2
    assert quality_gate["extraction_failure_count"] == 1
    assert quality_gate["failure_rate"] == 0.5
