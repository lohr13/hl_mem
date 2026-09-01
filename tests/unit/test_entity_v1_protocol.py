from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from benchmarks.release.entity_v1 import load_protocol, protocol_hash, validate_protocol

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_PATH = ROOT / "benchmarks" / "release" / "entity_v1_protocol.json"
BASELINE_PATH = ROOT / "benchmarks" / "release" / "results" / "entity-v1-baseline.json"
PHASE_2_PATH = ROOT / "benchmarks" / "release" / "results" / "entity-v1-1.1.0.json"

REQUIRED_CATEGORIES = {
    "unique_active_alias",
    "multilingual_alias",
    "alias_in_longer_text",
    "historical_alias",
    "cross_type_same_name",
    "same_span_ambiguity",
    "overlapping_alias",
    "multiple_entities",
    "no_entity",
    "incomplete_links",
    "empty_residual",
    "temporal_current",
    "temporal_historical",
    "namespace_isolation",
    "storage_failure",
}


def test_entity_protocol_has_exactly_24_unique_synthetic_cases() -> None:
    assert PROTOCOL_PATH.is_file(), "the frozen entity protocol has not been created"
    protocol = load_protocol(PROTOCOL_PATH)

    validate_protocol(protocol)
    cases = protocol["cases"]
    assert len(cases) == 24
    assert len({case["id"] for case in cases}) == 24
    assert {case["category"] for case in cases} >= REQUIRED_CATEGORIES
    assert protocol_hash(protocol) == protocol["fixture_sha256"]
    assert all(case["synthetic"] is True for case in cases)


def test_entity_baseline_is_bound_to_protocol_and_phase_1_commit() -> None:
    assert BASELINE_PATH.is_file(), "the Phase 1 entity baseline has not been captured"
    protocol = load_protocol(PROTOCOL_PATH)
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))

    assert baseline["schema_version"] == 1
    assert baseline["protocol_id"] == "hl-mem-entity-v1"
    assert baseline["fixture_sha256"] == protocol["fixture_sha256"]
    assert baseline["commit"] == "1f7e5cc23875dccb4503979c8a05733b8f069e97"
    assert baseline["mode"] == "observe"
    assert baseline["case_count"] == 24
    assert baseline["external_model_calls"] == 0
    assert baseline["llm_calls"] == 0
    assert baseline["reranker_calls"] == 0
    assert baseline["embedding_calls"] == 24
    assert len(baseline["cases"]) == 24


def _assert_phase_2_targets(candidate: dict[str, Any], baseline: dict[str, Any]) -> None:
    candidate_cases = {case["id"]: case for case in candidate["cases"]}
    baseline_cases = {case["id"]: case for case in baseline["cases"]}
    scoped_cases = [case for case in candidate_cases.values() if case["expected_scope"] == "entity"]
    wide_cases = [case for case in candidate_cases.values() if case["wide_equivalent"]]

    assert scoped_cases
    assert all(case["actual_scope"] == "entity" for case in scoped_cases)
    assert all(case["hit_at_5"] is True for case in scoped_cases)
    assert all(case["forbidden_top_1"] is False for case in scoped_cases)
    assert candidate["metrics"]["cross_entity_top_1_count"] == 0
    assert all(case["actual_claim_ids"] == baseline_cases[case["id"]]["actual_claim_ids"] for case in wide_cases)
    assert candidate["llm_calls"] == candidate["reranker_calls"] == candidate["external_model_calls"] == 0
    assert candidate["embedding_calls"] == baseline["embedding_calls"]
    baseline_p95 = float(baseline["latency_ms"]["p95"])
    assert float(candidate["latency_ms"]["p95"]) <= max(baseline_p95 + 10.0, baseline_p95 * 1.10)


def test_phase_2_entity_result_meets_frozen_target() -> None:
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    candidate = json.loads(PHASE_2_PATH.read_text(encoding="utf-8"))

    _assert_phase_2_targets(candidate, baseline)
