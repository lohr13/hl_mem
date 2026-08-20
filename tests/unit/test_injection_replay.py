from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from scripts.run_v0291_injection_replay import (
    build_fixture,
    load_fixture_spec,
    main,
    run_replay,
    write_expanded_fixture,
)

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "tests/fixtures/v0291_injection_replay.json"


def test_fixture_constructor_is_deterministic_complete_and_has_no_verified_at(tmp_path: Path) -> None:
    spec = load_fixture_spec(SPEC)
    first = build_fixture(spec)
    second = build_fixture(spec)

    assert first == second
    assert len(first) == 200
    assert len({point["point_id"] for point in first}) == 200
    assert Counter(point["cohort"] for point in first) == {
        "echo_recent": 30,
        "echo_boundary": 20,
        "cross_session": 20,
        "proper_noun_hard_negative": 20,
        "fail_open": 20,
        "stale_incident": 20,
        "stale_related": 20,
        "stable_fact": 20,
        "correction_backed_reference": 10,
        "packing_boundary": 10,
        "historical_and_active": 10,
    }
    assert "verified_at" not in json.dumps(first, sort_keys=True)

    expanded = tmp_path / "expanded.jsonl"
    digest = write_expanded_fixture(first, expanded)
    assert len(expanded.read_text(encoding="utf-8").splitlines()) == 200
    assert len(digest) == 64


def test_combined_replay_runs_policy_then_fixed_reranker_then_freshness_then_packing() -> None:
    spec = load_fixture_spec(SPEC)
    report = run_replay(spec, build_fixture(spec))

    assert report["schema_version"] == "v0291-injection-replay-v1"
    assert report["point_count"] == 200
    assert list(report["arms"]) == [
        "echo_off__freshness_off",
        "echo_enforce__freshness_off",
        "echo_off__freshness_render",
        "echo_enforce__freshness_render",
    ]
    assert report["pipeline_order"] == ["echo_filter", "fixed_reranker", "freshness_decorate", "packing"]
    assert report["shadow_invariants"] == {
        "echo_observe_equals_off": True,
        "freshness_observe_equals_off": True,
    }
    assert report["gates"]["structural_passed"] is True
    assert report["gates"]["online_quality_evaluation"] == "required_after_deployment"
    assert report["echo_metrics"]["echo_suppression_recall"] >= 0.8
    assert report["echo_metrics"]["false_suppression_rate"] <= 0.01
    assert report["echo_metrics"]["useful_retention"] >= 0.99
    assert report["freshness_metrics"]["maximum_added_tokens"] <= 18
    assert report["freshness_metrics"]["stable_fact_retention"] >= 0.98
    assert report["freshness_metrics"]["false_staleness_rate"] <= 0.01
    assert report["slice_equivalence"]["cross_session"] is True
    assert report["slice_equivalence"]["historical_and_active"] is True
    assert report["slice_equivalence"]["proper_noun_hard_negative"] is True
    assert all(len(arm["decisions"]) <= 1000 for arm in report["arms"].values())
    decisions = [decision for arm in report["arms"].values() for decision in arm["decisions"]]
    assert len(decisions) == 800
    assert all(
        [item["id"] for item in decision["context_packet"]["items"]] == decision["final_ids"] for decision in decisions
    )
    assert all(json.loads(decision["context_packet_text"]) == decision["context_packet"] for decision in decisions)
    assert all(
        [item["text"] for item in decision["context_packet"]["items"]] == decision["final_context_texts"]
        for decision in decisions
    )


def test_replay_cli_writes_report_and_optional_expanded_fixture(tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    expanded = tmp_path / "fixture.jsonl"

    result = main(
        [
            "--fixture",
            str(SPEC),
            "--output",
            str(output),
            "--export-expanded-fixture",
            str(expanded),
        ]
    )

    assert result == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["gates"]["structural_passed"] is True
    assert report["expanded_fixture"]["point_count"] == 200
    assert expanded.is_file()
