from __future__ import annotations

from evaluation.v0291_behavioral.report import build_evaluation_report
from scripts.run_v0291_behavioral_report import _markdown


def _structural() -> dict:
    decisions = [{"context_packet_text": "{}"}] * 200
    return {
        "point_count": 200,
        "arms": {
            "echo_off__freshness_off": {"decisions": decisions},
            "echo_enforce__freshness_off": {"decisions": decisions},
            "echo_off__freshness_render": {"decisions": decisions},
            "echo_enforce__freshness_render": {"decisions": decisions},
        },
        "gates": {"structural_passed": True},
        "echo_metrics": {
            "echo_suppression_recall": 0.9,
            "false_suppression_rate": 0.0,
            "useful_retention": 1.0,
            "source_session_resolution_rate": 1.0,
            "empty_packet_delta": 0,
        },
        "freshness_metrics": {
            "maximum_added_tokens": 10,
            "p95_added_tokens_to_budget": 0.02,
            "stable_fact_retention": 1.0,
            "false_staleness_rate": 0.0,
            "useful_item_retention": 1.0,
        },
        "slice_equivalence": {
            "cross_session": True,
            "historical_and_active": True,
            "proper_noun_hard_negative": True,
        },
    }


def _aggregate() -> dict:
    def metrics(unsafe: float, verification: float) -> dict:
        return {
            "unsafe_obsolete_acceptance": {"rate": unsafe},
            "verification_action_rate": {"rate": verification},
            "stable_fact_retention": {"rate": 1.0},
            "false_staleness_rate": {"rate": 0.0},
        }

    return {
        "expected_count": 320,
        "valid_count": 320,
        "arms": {
            "echo_off__freshness_off": metrics(0.6, 0.2),
            "echo_enforce__freshness_off": metrics(0.6, 0.2),
            "echo_off__freshness_render": metrics(0.1, 0.9),
            "echo_enforce__freshness_render": metrics(0.1, 0.9),
        },
        "slices": {
            "cohort": {
                "stable_negative": {
                    "echo_off__freshness_off": metrics(0.0, 0.0),
                    "echo_off__freshness_render": metrics(0.0, 0.0),
                }
            }
        },
    }


def test_report_separates_structural_behavioral_and_canary_conclusions() -> None:
    report = build_evaluation_report(
        structural=_structural(),
        sentinel={"passed": False, "valid_count": 0, "matched_count": 0},
        aggregate=None,
        blind_review=None,
        runtime_evidence=None,
    )

    assert report["conclusion"] == {
        "offline_structural_pass": True,
        "offline_behavioral_pass": False,
        "canary_ready": False,
    }
    statuses = {row["gate_id"]: row["status"] for row in report["gate_table"]}
    assert statuses["behavior.sentinel_9x9"] == "fail"
    assert statuses["freshness.unsafe_acceptance"] == "blocked"
    assert statuses["runtime.observe_window"] == "not_measured"


def test_offline_behavior_can_pass_but_runtime_evidence_still_forces_canary_false() -> None:
    report = build_evaluation_report(
        structural=_structural(),
        sentinel={"passed": True, "valid_count": 9, "matched_count": 9},
        aggregate=_aggregate(),
        blind_review={"required": 9, "completed": 9, "matched": 9},
        runtime_evidence=None,
    )

    assert report["conclusion"] == {
        "offline_structural_pass": True,
        "offline_behavioral_pass": True,
        "canary_ready": False,
    }
    stable_gate = next(row for row in report["gate_table"] if row["gate_id"] == "freshness.stable_retention")
    assert stable_gate["status"] == "pass"
    assert stable_gate["scope_caveat"] == ("20-case frozen acceptance suite; no population-rate extrapolation")


def test_canary_requires_every_named_runtime_gate() -> None:
    runtime = {
        "observe_window": True,
        "freshness_packet_p95": True,
        "freshness_renderer_p95": True,
        "echo_recall_p95": True,
        "echo_source_resolution": True,
    }
    report = build_evaluation_report(
        structural=_structural(),
        sentinel={"passed": True, "valid_count": 9, "matched_count": 9},
        aggregate=_aggregate(),
        blind_review={"required": 9, "completed": 9, "matched": 9},
        runtime_evidence=runtime,
    )

    assert report["conclusion"]["canary_ready"] is True
    del runtime["echo_recall_p95"]
    report = build_evaluation_report(
        structural=_structural(),
        sentinel={"passed": True, "valid_count": 9, "matched_count": 9},
        aggregate=_aggregate(),
        blind_review={"required": 9, "completed": 9, "matched": 9},
        runtime_evidence=runtime,
    )
    assert report["conclusion"]["canary_ready"] is False


def test_incomplete_aggregate_fails_closed_even_when_rates_pass() -> None:
    aggregate = _aggregate()
    aggregate["valid_count"] = 319
    report = build_evaluation_report(
        structural=_structural(),
        sentinel={"passed": True, "valid_count": 9, "matched_count": 9},
        aggregate=aggregate,
        blind_review={"required": 9, "completed": 9, "matched": 9},
        runtime_evidence=None,
    )

    assert report["conclusion"]["offline_behavioral_pass"] is False
    unsafe = next(row for row in report["gate_table"] if row["gate_id"] == "freshness.unsafe_acceptance")
    assert unsafe["status"] == "fail"


def test_markdown_reports_incremental_budget_without_claiming_full_phase_was_skipped() -> None:
    sentinel = {"passed": True, "valid_count": 9, "matched_count": 9}
    report = build_evaluation_report(
        structural=_structural(),
        sentinel=sentinel,
        aggregate=_aggregate(),
        blind_review=None,
        runtime_evidence=None,
    )

    markdown = _markdown(
        report,
        manifest={"model_snapshot": "model", "code_commit": "commit"},
        sentinel=sentinel,
        budget={
            "hard_budget_cny": 14.796848,
            "spent_cny": 0.00006,
            "actual_input_tokens": 10,
            "actual_output_tokens": 5,
            "reserved_cny": 0,
            "outstanding_reservations": 0,
        },
        artifact_hashes={},
    )

    assert "最后一次增量 provider usage：input=10, output=5" in markdown
    assert "最后一次增量估算实付：¥0.00006" in markdown
    assert "评测启动时 HEAD：`commit`" in markdown
    assert "预算硬上限：¥14.796848；reserved=0, outstanding=0" in markdown
    assert "本次没有启动全量付费阶段" not in markdown
