"""Three-part verdict and complete gate table for the v0.29.1 evaluation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

CONTROL_ARM = "echo_off__freshness_off"
FRESHNESS_ARM = "echo_off__freshness_render"
EXPECTED_ARMS = frozenset(
    {
        "echo_off__freshness_off",
        "echo_enforce__freshness_off",
        "echo_off__freshness_render",
        "echo_enforce__freshness_render",
    }
)
STABLE_SCOPE_CAVEAT = "20-case frozen acceptance suite; no population-rate extrapolation"
RUNTIME_GATES = (
    ("runtime.observe_window", "Production observe/canary evidence window completed"),
    ("runtime.freshness_packet_p95", "Freshness packet delta p95 on production traffic"),
    ("runtime.freshness_renderer_p95", "Freshness renderer latency p95 <= max(2ms, 2%)"),
    ("runtime.echo_recall_p95", "Echo recall latency p95 <= max(5ms, 5%)"),
    ("runtime.echo_source_resolution", "Echo source-session resolution >=95% and missing/read-error fail-open"),
)


def build_evaluation_report(
    *,
    structural: Mapping[str, Any] | None,
    sentinel: Mapping[str, Any] | None,
    aggregate: Mapping[str, Any] | None,
    blind_review: Mapping[str, Any] | None,
    runtime_evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build fail-closed structural, behavioral, and canary conclusions."""

    gate_table = _structural_gates(structural)
    structural_pass = bool(gate_table) and all(row["status"] == "pass" for row in gate_table)

    behavior_rows = _behavioral_gates(
        structural_pass=structural_pass,
        sentinel=sentinel,
        aggregate=aggregate,
        blind_review=blind_review,
    )
    gate_table.extend(behavior_rows)
    behavioral_pass = structural_pass and all(row["status"] == "pass" for row in behavior_rows)

    runtime_rows = _runtime_gates(runtime_evidence)
    gate_table.extend(runtime_rows)
    canary_ready = behavioral_pass and all(row["status"] == "pass" for row in runtime_rows)
    return {
        "schema_version": "v0291-evaluation-report-v1",
        "conclusion": {
            "offline_structural_pass": structural_pass,
            "offline_behavioral_pass": behavioral_pass,
            "canary_ready": canary_ready,
        },
        "gate_table": gate_table,
        "scope_notes": [
            STABLE_SCOPE_CAVEAT,
            "Synthetic structural timing and source-session signals do not replace production observe evidence.",
        ],
    }


def _structural_gates(structural: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if structural is None:
        return [
            _gate(gate_id, "structural", "blocked", threshold, None)
            for gate_id, threshold in (
                ("structure.full_200x4", "200 points x four exact arms"),
                ("structure.context_packet_body", "exact Context Packet body on all 800 decisions"),
                ("echo.suppression_recall", ">=80% gold echo suppression recall"),
                ("echo.useful_retention", ">=99% useful non-echo retention"),
                ("echo.slice_equivalence", "100% cross-session, historical/as-of, and hard-negative ID equivalence"),
                ("echo.false_suppression", "<=1% overall and zero on proper-noun/number hard negatives"),
                ("echo.empty_no_evidence", "empty packet delta <=1pp; no useful query becomes no_evidence"),
                ("freshness.claim_token_delta", "<=18 estimated tokens per annotated claim"),
                ("freshness.packet_budget_p95", "packet delta p95 <=3% of packed budget"),
                ("freshness.useful_no_evidence", ">=99% useful item retention; no useful query becomes no_evidence"),
            )
        ]

    arms = structural.get("arms")
    arms = arms if isinstance(arms, Mapping) else {}
    arm_counts = {
        str(name): len(arm.get("decisions", ()))
        for name, arm in arms.items()
        if isinstance(arm, Mapping) and isinstance(arm.get("decisions"), Sequence)
    }
    full_matrix = (
        structural.get("point_count") == 200 and set(arm_counts) == EXPECTED_ARMS and set(arm_counts.values()) == {200}
    )
    decisions = [
        decision
        for arm in arms.values()
        if isinstance(arm, Mapping)
        for decision in arm.get("decisions", ())
        if isinstance(decision, Mapping)
    ]
    exact_packets = len(decisions) == 800 and all(isinstance(row.get("context_packet_text"), str) for row in decisions)
    echo = _mapping(structural.get("echo_metrics"))
    fresh = _mapping(structural.get("freshness_metrics"))
    slices = _mapping(structural.get("slice_equivalence"))
    declared = _mapping(structural.get("gates")).get("structural_passed") is True

    return [
        _gate(
            "structure.full_200x4",
            "structural",
            _status(full_matrix and declared),
            "200 points x four exact arms",
            arm_counts,
        ),
        _gate(
            "structure.context_packet_body",
            "structural",
            _status(exact_packets),
            "exact Context Packet body on all 800 decisions",
            len(decisions),
        ),
        _gate(
            "echo.suppression_recall",
            "structural",
            _status(_at_least(echo.get("echo_suppression_recall"), 0.80)),
            ">=80% gold echo suppression recall",
            echo.get("echo_suppression_recall"),
        ),
        _gate(
            "echo.useful_retention",
            "structural",
            _status(_at_least(echo.get("useful_retention"), 0.99)),
            ">=99% useful non-echo retention",
            echo.get("useful_retention"),
        ),
        _gate(
            "echo.slice_equivalence",
            "structural",
            _status(bool(slices) and all(value is True for value in slices.values())),
            "100% cross-session, historical/as-of, and hard-negative ID equivalence",
            dict(slices),
        ),
        _gate(
            "echo.false_suppression",
            "structural",
            _status(
                _at_most(echo.get("false_suppression_rate"), 0.01) and slices.get("proper_noun_hard_negative") is True
            ),
            "<=1% overall and zero on proper-noun/number hard negatives",
            echo.get("false_suppression_rate"),
        ),
        _gate(
            "echo.empty_no_evidence",
            "structural",
            _status(_at_most(echo.get("empty_packet_delta"), 0.01) and _at_least(echo.get("useful_retention"), 0.99)),
            "empty packet delta <=1pp; no useful query becomes no_evidence",
            echo.get("empty_packet_delta"),
        ),
        _gate(
            "freshness.claim_token_delta",
            "structural",
            _status(_at_most(fresh.get("maximum_added_tokens"), 18)),
            "<=18 estimated tokens per annotated claim",
            fresh.get("maximum_added_tokens"),
        ),
        _gate(
            "freshness.packet_budget_p95",
            "structural",
            _status(_at_most(fresh.get("p95_added_tokens_to_budget"), 0.03)),
            "packet delta p95 <=3% of packed budget",
            fresh.get("p95_added_tokens_to_budget"),
        ),
        _gate(
            "freshness.useful_no_evidence",
            "structural",
            _status(_at_least(fresh.get("useful_item_retention"), 0.99)),
            ">=99% useful item retention; no useful query becomes no_evidence",
            fresh.get("useful_item_retention"),
        ),
    ]


def _behavioral_gates(
    *,
    structural_pass: bool,
    sentinel: Mapping[str, Any] | None,
    aggregate: Mapping[str, Any] | None,
    blind_review: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    sentinel_ok = bool(
        sentinel
        and sentinel.get("passed") is True
        and sentinel.get("valid_count") == 9
        and sentinel.get("matched_count") == 9
    )
    rows = [
        _gate(
            "behavior.sentinel_9x9",
            "behavioral",
            _status(sentinel_ok) if sentinel is not None else "blocked",
            "9/9 valid schema/evidence/applicability and 9/9 gold match",
            _select(sentinel, "valid_count", "matched_count", "passed"),
        )
    ]
    prerequisites = structural_pass and sentinel_ok
    blind_ok = bool(
        blind_review
        and blind_review.get("required") == 9
        and blind_review.get("completed") == 9
        and blind_review.get("matched") == 9
    )
    rows.append(
        _gate(
            "behavior.blind_review_9",
            "behavioral",
            _status(blind_ok) if prerequisites and blind_review is not None else "blocked",
            "3 stale + 3 stable + 3 boundary real traces manually reviewed and matched",
            dict(blind_review) if blind_review else None,
        )
    )
    if not prerequisites or aggregate is None:
        for gate_id, threshold in (
            ("freshness.unsafe_acceptance", "treatment <=10% and >=50% relative reduction from control"),
            ("freshness.verification_action", "treatment verification action rate >=80%"),
            ("freshness.stable_retention", "frozen stable suite >=98% and <=2pp below control"),
            ("freshness.false_staleness", "stable preference/identity increment <=1pp"),
        ):
            row = _gate(gate_id, "behavioral", "blocked", threshold, None)
            if gate_id == "freshness.stable_retention":
                row["scope_caveat"] = STABLE_SCOPE_CAVEAT
            rows.append(row)
        return rows

    complete = aggregate.get("valid_count") == aggregate.get("expected_count") == 320
    arms = _mapping(aggregate.get("arms"))
    control = _mapping(arms.get(CONTROL_ARM))
    treatment = _mapping(arms.get(FRESHNESS_ARM))
    unsafe_control = _rate(control, "unsafe_obsolete_acceptance")
    unsafe_treatment = _rate(treatment, "unsafe_obsolete_acceptance")
    relative = _relative_reduction(unsafe_control, unsafe_treatment)
    verification = _rate(treatment, "verification_action_rate")

    stable_arms = _cohort_arms(aggregate, "stable_negative")
    stable_control = _rate(_mapping(stable_arms.get(CONTROL_ARM)), "stable_fact_retention")
    stable_treatment = _rate(_mapping(stable_arms.get(FRESHNESS_ARM)), "stable_fact_retention")
    false_control = _rate(_mapping(stable_arms.get(CONTROL_ARM)), "false_staleness_rate")
    false_treatment = _rate(_mapping(stable_arms.get(FRESHNESS_ARM)), "false_staleness_rate")

    rows.extend(
        [
            _gate(
                "freshness.unsafe_acceptance",
                "behavioral",
                _status(complete and _at_most(unsafe_treatment, 0.10) and _at_least(relative, 0.50)),
                "treatment <=10% and >=50% relative reduction from control",
                {"control": unsafe_control, "treatment": unsafe_treatment, "relative_reduction": relative},
            ),
            _gate(
                "freshness.verification_action",
                "behavioral",
                _status(complete and _at_least(verification, 0.80)),
                "treatment verification action rate >=80%",
                verification,
            ),
            _gate(
                "freshness.stable_retention",
                "behavioral",
                _status(
                    complete
                    and _at_least(stable_treatment, 0.98)
                    and _difference_at_least(stable_treatment, stable_control, -0.02)
                ),
                "frozen stable suite >=98% and <=2pp below control",
                {"control": stable_control, "treatment": stable_treatment},
                scope_caveat=STABLE_SCOPE_CAVEAT,
            ),
            _gate(
                "freshness.false_staleness",
                "behavioral",
                _status(complete and _difference_at_most(false_treatment, false_control, 0.01)),
                "stable preference/identity increment <=1pp",
                {"control": false_control, "treatment": false_treatment},
                scope_caveat=STABLE_SCOPE_CAVEAT,
            ),
        ]
    )
    return rows


def _runtime_gates(runtime_evidence: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    rows = []
    for gate_id, threshold in RUNTIME_GATES:
        key = gate_id.removeprefix("runtime.")
        if runtime_evidence is None or key not in runtime_evidence:
            status = "not_measured"
            observed = None
        else:
            observed = runtime_evidence[key]
            status = _status(observed is True)
        rows.append(_gate(gate_id, "runtime", status, threshold, observed))
    return rows


def _cohort_arms(aggregate: Mapping[str, Any], cohort: str) -> Mapping[str, Any]:
    slices = _mapping(aggregate.get("slices"))
    cohorts = _mapping(slices.get("cohort"))
    return _mapping(cohorts.get(cohort))


def _rate(metrics: Mapping[str, Any], name: str) -> float | None:
    metric = metrics.get(name)
    if not isinstance(metric, Mapping):
        return None
    value = metric.get("rate")
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _relative_reduction(control: float | None, treatment: float | None) -> float | None:
    if control is None or treatment is None or control <= 0:
        return None
    return (control - treatment) / control


def _difference_at_least(left: float | None, right: float | None, threshold: float) -> bool:
    return left is not None and right is not None and left - right >= threshold - 1e-12


def _difference_at_most(left: float | None, right: float | None, threshold: float) -> bool:
    return left is not None and right is not None and left - right <= threshold + 1e-12


def _at_least(value: Any, threshold: float) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value >= threshold


def _at_most(value: Any, threshold: float) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value <= threshold


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _select(value: Mapping[str, Any] | None, *keys: str) -> dict[str, Any] | None:
    return {key: value.get(key) for key in keys} if value is not None else None


def _status(passed: bool) -> str:
    return "pass" if passed else "fail"


def _gate(
    gate_id: str,
    category: str,
    status: str,
    threshold: str,
    observed: Any,
    *,
    scope_caveat: str | None = None,
) -> dict[str, Any]:
    row = {
        "gate_id": gate_id,
        "category": category,
        "status": status,
        "threshold": threshold,
        "observed": observed,
    }
    if scope_caveat is not None:
        row["scope_caveat"] = scope_caveat
    return row
