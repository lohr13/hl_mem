from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).with_name("run_glm_effort_low_20cases.py")


def load_runner():
    assert SCRIPT.is_file(), "run_glm_effort_low_20cases.py must implement arm D"
    spec = importlib.util.spec_from_file_location("glm_effort_low_runner", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_payload_is_exact_official_glm_effort_low_shape() -> None:
    runner = load_runner()

    payload = runner.build_payload(
        [{"role": "user", "content": "remember this"}],
        language="en",
    )

    assert payload["model"] == "glm-5.3-flash"
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "low"
    assert payload["messages"][0]["role"] == "system"
    assert "Coverage first:" in payload["messages"][0]["content"]
    assert payload["response_format"]["json_schema"]["schema"]["properties"]["claims"]["maxItems"] == 30
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert "max_tokens" not in payload
    assert "enable_thinking" not in payload
    assert "thinking_budget" not in payload


def test_usage_cost_uses_measured_half_price_glm_rates() -> None:
    runner = load_runner()

    cost = runner.usage_cost_cny(
        {
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "reasoning_tokens": 1024,
            "cached_tokens": 999_999,
        }
    )

    assert cost == pytest.approx(1.8)


def test_gate_report_uses_corrected_coverage_and_separate_misbinding_taxonomy() -> None:
    runner = load_runner()
    case_ids = [f"case-{index:02d}" for index in range(20)]
    manifest = {
        "selection": {"key_fact_case_ids": case_ids[:5]},
        "cases": [
            {"case_id": case_id, "case_type": "dense"}
            for case_id in case_ids
        ],
    }
    runs = []
    for index, case_id in enumerate(case_ids):
        claims = [
            {"subject": f"s-{claim}", "value": f"v-{claim}", "kind": "fact"}
            for claim in range(12)
        ]
        runs.append(
            {
                "case_id": case_id,
                "arm": "D",
                "request_started": True,
                "attempt_count": 1,
                "status": "success",
                "schema_valid": True,
                "latency_seconds": float(20 + index),
                "claims": claims,
                "claim_count": len(claims),
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 2000,
                    "reasoning_tokens": 15,
                    "cached_tokens": 0,
                },
                "cost_cny": 0.01,
                "duplicate_profile": runner.base.duplicate_profile(claims),
            }
        )
    review_cases = []
    for index, case_id in enumerate(case_ids[:5]):
        included = index > 0
        review_cases.append(
            {
                "case_id": case_id,
                "reviewed": True,
                "coverage_gate_included": included,
                "facts": [
                    {"id": f"f{fact:02d}", "text": f"fact {fact}", "covered": not (included and index == 1 and fact == 0)}
                    for fact in range(10)
                ],
                "hallucinations": [],
                "subject_misbindings": ([{"claim_index": 1, "note": "wrong owner"}] if index == 1 else []),
            }
        )
    manual_review = {
        "key_fact_review": {
            "reviewed_case_count": 5,
            "coverage_case_count": 4,
            "cases": review_cases,
        }
    }

    report = runner.score_records(manifest, runs, manual_review)
    gates = {gate["gate_id"]: gate for gate in report["gates"]}

    assert report["overall_passed"] is True
    assert gates["density"]["measured"]["qualifying_cases"] == 20
    assert gates["latency_p50"]["threshold"] == "<=40s"
    assert gates["latency_p95"]["threshold"] == "<=90s"
    assert gates["reasoning_tokens"]["measured"]["max"] == 15
    assert gates["cost_mean"]["measured"] == pytest.approx(0.01)
    assert gates["schema_success"]["measured"] == pytest.approx(1.0)
    assert gates["duplicate_rate"]["measured"]["rate"] == pytest.approx(0.0)
    assert gates["hallucination"]["measured"]["hallucinated_claims"] == 0
    assert gates["subject_misbinding"]["measured"]["claims"] == 1
    assert gates["subject_misbinding"]["passed"] is True
    assert gates["gold_coverage_corrected"]["measured"] == {
        "covered": 39,
        "total": 40,
        "coverage": 0.975,
    }
    assert gates["run_integrity"]["passed"] is True


def test_prepare_review_template_preserves_entire_frozen_gold_definition(tmp_path: Path) -> None:
    runner = load_runner()
    case_ids = [f"case-{index}" for index in range(5)]
    manifest = {
        "selection": {"key_fact_case_ids": case_ids},
        "cases": [{"case_id": case_id, "case_type": "dense"} for case_id in case_ids],
    }
    gold = {
        "key_fact_review": {
            "cases": [
                {
                    "case_id": case_id,
                    "coverage_gate_included": index > 0,
                    "facts": [{"id": "f01", "text": f"gold {index}"}],
                }
                for index, case_id in enumerate(case_ids)
            ]
        },
        "short_event_review": {
            "cases": [
                {"case_id": "short-1", "expected": {"maximum_claims": 1}}
            ]
        },
    }
    runs = [
        {
            "case_id": case_id,
            "arm": "D",
            "claims": [],
        }
        for case_id in case_ids
    ]
    manifest_path = tmp_path / "manifest.json"
    gold_path = tmp_path / "gold.json"
    runs_path = tmp_path / "runs.jsonl"
    review_path = tmp_path / "review.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    gold_path.write_text(json.dumps(gold), encoding="utf-8")
    runs_path.write_text("\n".join(json.dumps(row) for row in runs) + "\n", encoding="utf-8")

    review = runner.prepare_review_template(manifest_path, runs_path, gold_path, review_path)

    assert runner.base.gold_definition_sha256(review) == runner.base.gold_definition_sha256(gold)
    assert review["short_event_review"] == gold["short_event_review"]
