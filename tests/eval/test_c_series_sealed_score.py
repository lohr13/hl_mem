"""C-series sealed matrix aggregation and gates."""

from __future__ import annotations

import pytest

from evaluation.tools.score_c_series_sealed_experiment import (
    aggregate_matrix,
    assert_exact_matrix,
    assert_implementation_snapshot,
    assert_raw_bindings,
    assert_raw_matches_packets,
)


def _row(case_id: str, arm: str, reader: str, correct: bool, *, coverage: float, latency: float = 1.0) -> dict:
    return {
        "case_id": case_id,
        "category": "cross_event_two_hop",
        "arm_id": arm,
        "reader_id": reader,
        "repeat_index": 0,
        "score": {
            "answer_correct": correct,
            "entity_coverage_at_5": coverage,
            "negative_violation": False,
            "role_modality_confusion": False,
            "modality_violation": False,
            "provenance_violation": False,
            "leakage_violation": False,
        },
        "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        "packet_tokens": 20,
        "recall_latency_seconds": latency,
        "reader_latency_seconds": 2.0,
        "e2e_latency_seconds": 3.0,
        "packet_budget_violation": False,
    }


def _three(row: dict) -> list[dict]:
    return [{**row, "repeat_index": repeat} for repeat in range(3)]


def test_c4_passes_sealed_gate_with_two_net_gains_and_no_regression() -> None:
    rows: list[dict] = []
    for reader in ("qwen", "glm"):
        rows += _three(_row("case-1", "C0", reader, False, coverage=0.0))
        rows += _three(_row("case-1", "C4", reader, True, coverage=1.0, latency=1.05))
        rows += _three(_row("case-2", "C0", reader, False, coverage=0.0))
        rows += _three(_row("case-2", "C4", reader, True, coverage=1.0, latency=1.05))
        rows += _three(_row("case-3", "C0", reader, True, coverage=1.0))
        rows += _three(_row("case-3", "C4", reader, True, coverage=1.0, latency=1.05))

    report = aggregate_matrix(rows, no_answer_ids=set())

    for reader in ("qwen", "glm"):
        assert report["gates"][reader]["hard_relation_net_gain_ge_2"] is True
        assert report["gates"][reader]["paired_zero_regression"] is True
        assert report["gates"][reader]["passed"] is True


def test_any_c0_correct_to_c4_incorrect_fails_paired_gate() -> None:
    rows: list[dict] = []
    for reader in ("qwen", "glm"):
        rows += _three(_row("case-1", "C0", reader, True, coverage=1.0))
        rows += _three(_row("case-1", "C4", reader, False, coverage=1.0))

    report = aggregate_matrix(rows, no_answer_ids=set())

    assert report["gates"]["qwen"]["correct_to_incorrect"] == 1
    assert report["gates"]["qwen"]["paired_zero_regression"] is False
    assert report["gates"]["qwen"]["passed"] is False


def test_any_baseline_safety_violation_fails_matrix_gate() -> None:
    rows: list[dict] = []
    for reader in ("qwen", "glm"):
        c0 = _row("case-1", "C0", reader, True, coverage=1.0)
        c0["score"] = {**c0["score"], "negative_violation": True}
        rows += _three(c0)
        rows += _three(_row("case-1", "C4", reader, True, coverage=1.0))

    report = aggregate_matrix(rows, no_answer_ids=set())

    assert report["gates"]["qwen"]["forbidden_zero"] is False
    assert report["gates"]["qwen"]["passed"] is False


def test_raw_packet_must_equal_frozen_packet_snapshot() -> None:
    raw = [
        {
            "case_id": "case-1",
            "repeat_index": 0,
            "arm_id": "C4",
            "reader_id": "glm",
            "packet": [{"claim_id": "changed"}],
            "top5_seed_packet": [],
            "answerability": "ok",
            "recall_latency_seconds": 1.0,
        }
    ]
    packets = {
        "packets": [
            {
                "packet_key": "case-1|0|C4",
                "packet": [{"claim_id": "frozen"}],
                "top5_seed_packet": [],
                "answerability": "ok",
                "recall_latency_seconds": 1.0,
            }
        ]
    }

    with pytest.raises(RuntimeError, match="packet differs"):
        assert_raw_matches_packets(raw, packets)


def test_exact_matrix_rejects_out_of_range_repeat_even_when_count_matches() -> None:
    rows = [
        {
            "case_id": "case-1",
            "repeat_index": repeat,
            "arm_id": arm,
            "reader_id": reader,
        }
        for repeat in range(3)
        for arm in ("C0", "C4")
        for reader in ("qwen", "glm")
    ]
    rows[-1] = {**rows[-1], "repeat_index": 3}

    with pytest.raises(RuntimeError, match="matrix keys"):
        assert_exact_matrix(rows, {"cases": [{"case_id": "case-1"}]})


def test_raw_rows_must_bind_preregistration_and_reader_snapshot() -> None:
    prereg = {
        "preregistration_id": "sealed-v1",
        "models": {
            "readers": {
                "qwen": {"model": "qwen3.7-plus"},
                "glm": {"model": "glm-5.3"},
            }
        },
    }
    rows = [
        {
            "case_id": "case-1",
            "repeat_index": 0,
            "arm_id": "C4",
            "reader_id": "glm",
            "preregistration_id": "different-run",
            "reader_snapshot_sha256": "0" * 64,
        }
    ]

    with pytest.raises(RuntimeError, match="preregistration binding"):
        assert_raw_bindings(rows, prereg)


def test_scorer_rejects_implementation_hash_drift() -> None:
    with pytest.raises(RuntimeError, match="implementation snapshot drift"):
        assert_implementation_snapshot({"implementation_snapshot": {"version": "changed"}})
