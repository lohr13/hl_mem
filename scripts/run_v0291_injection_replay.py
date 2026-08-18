#!/usr/bin/env python
"""Build and run the deterministic v0.29.1 echo × freshness bundle replay."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence, cast

from hl_mem.application.context_packet import (
    RetrievalBundle,
    RetrievalBundleItem,
    apply_freshness_decisions,
    pack_retrieval_bundle,
)
from hl_mem.recall.echo_suppression import EchoRequest, EchoSuppressionPolicy
from hl_mem.recall.freshness_annotation import (
    FreshnessAnnotationPolicy,
    FreshnessItem,
    FreshnessRequest,
)
from hl_mem.recall.injection import ECHO_POLICY_VERSION, FRESHNESS_POLICY_VERSION, DeliveryPurpose

REPLAY_SCHEMA_VERSION = "v0291-injection-replay-v1"
FIXTURE_SCHEMA_VERSION = "v0291-injection-fixture-v1"
PIPELINE_ORDER = ("echo_filter", "fixed_reranker", "freshness_decorate", "packing")
ARM_SPECS = (
    ("echo_off__freshness_off", "off", "off"),
    ("echo_enforce__freshness_off", "enforce", "off"),
    ("echo_off__freshness_render", "off", "render"),
    ("echo_enforce__freshness_render", "enforce", "render"),
)
EXPECTED_COHORTS = {
    "echo_recent",
    "echo_boundary",
    "cross_session",
    "proper_noun_hard_negative",
    "fail_open",
    "stale_incident",
    "stale_related",
    "stable_fact",
    "correction_backed_reference",
    "packing_boundary",
    "historical_and_active",
}


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("fixture rendering_now must include a timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def load_fixture_spec(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != FIXTURE_SCHEMA_VERSION:
        raise ValueError("unsupported injection replay fixture schema")
    cohorts = payload.get("cohorts")
    if not isinstance(cohorts, list):
        raise ValueError("fixture cohorts must be a list")
    names = {item.get("name") for item in cohorts if isinstance(item, dict)}
    if names != EXPECTED_COHORTS:
        raise ValueError(f"fixture cohorts do not match the frozen set: {sorted(names)}")
    if any(not isinstance(item.get("count"), int) or item["count"] < 1 for item in cohorts):
        raise ValueError("fixture cohort counts must be positive integers")
    if sum(int(item["count"]) for item in cohorts) < 200:
        raise ValueError("fixture must contain at least 200 recall points")
    _timestamp(str(payload.get("rendering_now", "")))
    if int(payload.get("default_token_budget", 0)) < 1:
        raise ValueError("fixture default_token_budget must be positive")
    if not 1 <= int(payload.get("artifact_decision_limit_per_arm", 0)) <= 1000:
        raise ValueError("fixture artifact decision limit must be between 1 and 1000")
    return payload


def _candidate(
    candidate_id: str,
    *,
    text: str,
    recorded_from: str,
    canonical_slot: str | None,
    canonical_attribute: str | None,
    topic_tags: list[str],
    gold_label: str,
    useful: bool,
    signal: dict[str, object],
    rerank_position: int,
) -> dict[str, Any]:
    return {
        "id": candidate_id,
        "type": "claim",
        "text": text,
        "recorded_from": recorded_from,
        "canonical_slot": canonical_slot,
        "canonical_attribute": canonical_attribute,
        "topic_tags": topic_tags,
        "gold_label": gold_label,
        "useful": useful,
        "signal": signal,
        "rerank_position": rerank_position,
    }


def _base_point(spec: dict[str, Any], cohort: str, index: int, now: datetime) -> dict[str, Any]:
    point_id = f"{cohort}-{index:03d}"
    fallback = _candidate(
        f"{point_id}-fallback",
        text="Preserve transactional evidence and fail closed on destructive changes.",
        recorded_from=_iso(now - timedelta(days=730)),
        canonical_slot="identity.constraint",
        canonical_attribute="identity.constraint",
        topic_tags=["identity"],
        gold_label="useful_cross_session",
        useful=True,
        signal={"source_session_resolved": True, "matching_session_recorded_at": None},
        rerank_position=1,
    )
    return {
        "point_id": point_id,
        "cohort": cohort,
        "query": f"fixed synthetic query for {cohort} {index}",
        "namespace": str(spec["namespace"]),
        "session_id": str(spec["session_id"]),
        "delivery_purpose": "passive_injection",
        "intent": "current_state",
        "as_of": None,
        "known_as_of": None,
        "rendering_now": _iso(now),
        "token_budget": int(spec["default_token_budget"]),
        "cost_gate_eligible": True,
        "candidates": [fallback],
    }


def _primary_for_cohort(point: dict[str, Any], cohort: str, index: int, now: datetime) -> dict[str, Any]:
    point_id = str(point["point_id"])
    resolved_no_match = {"source_session_resolved": True, "matching_session_recorded_at": None}
    if cohort == "echo_recent":
        return _candidate(
            f"{point_id}-primary",
            text=f"The current deployment source is checkout-{index}.",
            recorded_from=_iso(now - timedelta(days=7)),
            canonical_slot="config.path",
            canonical_attribute="config.path",
            topic_tags=["config", "implementation"],
            gold_label="echo",
            useful=False,
            signal={
                "source_session_resolved": True,
                "matching_session_recorded_at": _iso(now - timedelta(seconds=60 + index % 1500)),
            },
            rerank_position=0,
        )
    if cohort == "echo_boundary":
        inside = index % 2 == 0
        return _candidate(
            f"{point_id}-primary",
            text=f"Boundary deployment fact {index}.",
            recorded_from=_iso(now - timedelta(days=1)),
            canonical_slot="state.deployment",
            canonical_attribute="state.deployment",
            topic_tags=["state"],
            gold_label="echo" if inside else "useful_same_session",
            useful=not inside,
            signal={
                "source_session_resolved": True,
                "matching_session_recorded_at": _iso(now - timedelta(seconds=1799 if inside else 1800)),
            },
            rerank_position=0,
        )
    if cohort == "cross_session":
        return _candidate(
            f"{point_id}-primary",
            text=f"Cross-session service state {index} remains useful.",
            recorded_from=_iso(now - timedelta(days=1)),
            canonical_slot="state.service_health",
            canonical_attribute="state.service_health",
            topic_tags=["state"],
            gold_label="useful_cross_session",
            useful=True,
            signal=resolved_no_match,
            rerank_position=0,
        )
    if cohort == "proper_noun_hard_negative":
        return _candidate(
            f"{point_id}-primary",
            text=f"Service Project-{index:02d} listens on port {8100 + index} at /srv/project-{index:02d}.",
            recorded_from=_iso(now - timedelta(days=30)),
            canonical_slot="config.network",
            canonical_attribute="config.network",
            topic_tags=["config", "dependency"],
            gold_label="useful_cross_session",
            useful=True,
            signal={
                **resolved_no_match,
                "pending_similarity": 0.99,
                "pending_created_at": _iso(now - timedelta(minutes=5)),
            },
            rerank_position=0,
        )
    if cohort == "fail_open":
        if index >= 18:
            point["session_id"] = None
        return _candidate(
            f"{point_id}-primary",
            text=f"Evidence is incomplete for claim {index}; retain it.",
            recorded_from=_iso(now - timedelta(days=7)),
            canonical_slot="config.model",
            canonical_attribute="config.model",
            topic_tags=["config"],
            gold_label="useful_same_session",
            useful=True,
            signal=(
                {"source_session_resolved": False, "matching_session_recorded_at": None}
                if index < 18
                else resolved_no_match
            ),
            rerank_position=0,
        )
    if cohort == "stale_incident":
        return _candidate(
            f"{point_id}-primary",
            text="Editable checkout controls the installed runtime; git checkout upgrades it.",
            recorded_from=_iso(now - timedelta(days=180)),
            canonical_slot="config.path",
            canonical_attribute="config.path",
            topic_tags=["config", "implementation"],
            gold_label="useful_cross_session",
            useful=True,
            signal=resolved_no_match,
            rerank_position=0,
        )
    if cohort == "stale_related":
        ages = (
            timedelta(hours=6),
            timedelta(days=1),
            timedelta(days=7),
            timedelta(days=30),
            timedelta(days=180),
            timedelta(days=730),
        )
        point["intent"] = "procedure"
        return _candidate(
            f"{point_id}-primary",
            text=f"Release procedure variant {index} must verify the current manifest before writing.",
            recorded_from=_iso(now - ages[index % len(ages)]),
            canonical_slot="custom.unknown",
            canonical_attribute="procedure.release",
            topic_tags=["implementation", "behavior"],
            gold_label="useful_cross_session",
            useful=True,
            signal=resolved_no_match,
            rerank_position=0,
        )
    if cohort == "stable_fact":
        if index % 2 == 0:
            point["intent"] = "procedure"
        stable_kind = "preference" if index % 2 == 0 else "identity"
        return _candidate(
            f"{point_id}-primary",
            text=f"Long-lived {stable_kind} fact {index} remains authoritative.",
            recorded_from=_iso(now - timedelta(days=365 + index * 20)),
            canonical_slot=f"{stable_kind}.response_style",
            canonical_attribute=f"{stable_kind}.response_style",
            topic_tags=[stable_kind],
            gold_label="useful_cross_session",
            useful=True,
            signal=resolved_no_match,
            rerank_position=0,
        )
    if cohort == "correction_backed_reference":
        candidate = _candidate(
            f"{point_id}-primary",
            text=f"Correction-backed release fact {index} records an explicit replacement source.",
            recorded_from=_iso(now - timedelta(days=7)),
            canonical_slot="config.release",
            canonical_attribute="config.release",
            topic_tags=["config", "bugfix"],
            gold_label="useful_cross_session",
            useful=True,
            signal=resolved_no_match,
            rerank_position=0,
        )
        candidate["fixture_source_kind"] = "correction_event"
        return candidate
    if cohort == "packing_boundary":
        point["token_budget"] = 22
        point["cost_gate_eligible"] = False
        point["candidates"][0]["text"] = "safe"
        return _candidate(
            f"{point_id}-primary",
            text="old procedure",
            recorded_from=_iso(now - timedelta(days=30)),
            canonical_slot="config.tool",
            canonical_attribute="config.tool",
            topic_tags=["config", "tool_choice"],
            gold_label="irrelevant",
            useful=False,
            signal=resolved_no_match,
            rerank_position=0,
        )
    if cohort == "historical_and_active":
        if index % 2 == 0:
            point["intent"] = "historical"
            point["as_of"] = _iso(now - timedelta(days=30))
        else:
            point["delivery_purpose"] = "active_recall"
        return _candidate(
            f"{point_id}-primary",
            text=f"Explicit historical or active fact {index} must bypass injection governance.",
            recorded_from=_iso(now - timedelta(days=365)),
            canonical_slot="config.history",
            canonical_attribute="memory.explicit",
            topic_tags=["config"],
            gold_label="useful_cross_session",
            useful=True,
            signal={
                "source_session_resolved": True,
                "matching_session_recorded_at": _iso(now - timedelta(minutes=5)),
            },
            rerank_position=0,
        )
    raise ValueError(f"unsupported fixture cohort: {cohort}")


def build_fixture(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand the compact committed spec into deterministic recall points."""
    if spec.get("schema_version") != FIXTURE_SCHEMA_VERSION:
        raise ValueError("unsupported injection replay fixture schema")
    now = _timestamp(str(spec["rendering_now"]))
    points: list[dict[str, Any]] = []
    for cohort_spec in spec["cohorts"]:
        cohort = str(cohort_spec["name"])
        for index in range(int(cohort_spec["count"])):
            point = _base_point(spec, cohort, index, now)
            primary = _primary_for_cohort(point, cohort, index, now)
            point["candidates"].insert(0, primary)
            points.append(point)
    return points


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def write_expanded_fixture(points: list[dict[str, Any]], path: Path) -> str:
    """Write the auditable expanded JSONL fixture and return its content hash."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(f"{_canonical_json(point)}\n" for point in points)
    path.write_text(content, encoding="utf-8")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _evaluate_point(point: dict[str, Any], *, echo_mode: str, freshness_mode: str) -> dict[str, Any]:
    candidates = cast(list[dict[str, Any]], point["candidates"])
    purpose = cast(DeliveryPurpose, point["delivery_purpose"])
    echo = EchoSuppressionPolicy(mode=cast(Any, echo_mode)).evaluate(
        [str(item["id"]) for item in candidates],
        EchoRequest(
            delivery_purpose=purpose,
            session_id=cast(str | None, point["session_id"]),
            namespace=str(point["namespace"]),
            intent=str(point["intent"]),
            as_of=cast(str | None, point["as_of"]),
            known_as_of=cast(str | None, point["known_as_of"]),
            request_now=str(point["rendering_now"]),
            experiment_variant=f"echo-{echo_mode}",
        ),
        {str(item["id"]): cast(dict[str, object], item["signal"]) for item in candidates},
    )
    suppressed = {decision.claim_id for decision in echo.decisions if decision.suppress}
    retained = [item for item in candidates if str(item["id"]) not in suppressed]
    reranked = sorted(retained, key=lambda item: (int(item["rerank_position"]), str(item["id"])))
    bundle = RetrievalBundle(
        query_id=str(point["point_id"]),
        answerability="supported",
        items=tuple(
            RetrievalBundleItem(
                type=cast(Any, item["type"]),
                id=str(item["id"]),
                text=str(item["text"]),
                score=1.0 - int(item["rerank_position"]) / 10,
            )
            for item in reranked
        ),
    )
    freshness = FreshnessAnnotationPolicy(mode=cast(Any, freshness_mode)).evaluate(
        [
            FreshnessItem(
                item_id=str(item["id"]),
                memory_type=str(item["type"]),
                text=str(item["text"]),
                recorded_from=cast(str | None, item["recorded_from"]),
                canonical_slot=cast(str | None, item["canonical_slot"]),
                canonical_attribute=cast(str | None, item["canonical_attribute"]),
                topic_tags=tuple(str(tag) for tag in item["topic_tags"]),
            )
            for item in reranked
        ],
        FreshnessRequest(
            delivery_purpose=purpose,
            intent=str(point["intent"]),
            as_of=cast(str | None, point["as_of"]),
            known_as_of=cast(str | None, point["known_as_of"]),
            rendering_now=str(point["rendering_now"]),
            experiment_variant=f"freshness-{freshness_mode}",
        ),
    )
    decorated = apply_freshness_decisions(bundle, freshness)
    packed = pack_retrieval_bundle(decorated, int(point["token_budget"]))
    eligible = [decision for decision in freshness.decisions if decision.eligible]
    return {
        "point_id": point["point_id"],
        "cohort": point["cohort"],
        "final_ids": [item.id for item in packed.items],
        "suppressed_ids": sorted(suppressed),
        "would_suppress_ids": sorted(decision.claim_id for decision in echo.decisions if decision.would_suppress),
        "freshness_eligible_ids": [decision.item_id for decision in eligible],
        "freshness_rendered_ids": [decision.item_id for decision in eligible] if freshness_mode == "render" else [],
        "added_tokens_by_id": {decision.item_id: decision.added_token_estimate for decision in eligible},
        "used_tokens": packed.used_tokens_estimate,
        "truncated": packed.truncated,
        "source_session_resolved": echo.source_session_resolved,
        "source_session_missing": echo.source_session_missing,
        "echo_bypass_reason": echo.bypass_reason,
        "echo_fail_open_reason": echo.fail_open_reason,
        "freshness_bypass_reason": freshness.bypass_reason,
    }


def _id_metadata(points: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(candidate["id"]): candidate
        for point in points
        for candidate in cast(list[dict[str, Any]], point["candidates"])
    }


def _final_ids(decisions: list[dict[str, Any]], cohorts: set[str] | None = None) -> list[str]:
    return [
        str(item_id)
        for decision in decisions
        if cohorts is None or str(decision["cohort"]) in cohorts
        for item_id in decision["final_ids"]
    ]


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * percentile + 0.999999)))
    return ordered[index]


def run_replay(spec: dict[str, Any], points: list[dict[str, Any]]) -> dict[str, Any]:
    """Run the four fixed arms plus non-mutating observe shadow invariants."""
    limit = int(spec["artifact_decision_limit_per_arm"])
    if len(points) > limit:
        raise ValueError(f"fixture has {len(points)} points, exceeding per-arm artifact limit {limit}")
    arms: dict[str, dict[str, Any]] = {}
    for name, echo_mode, freshness_mode in ARM_SPECS:
        decisions = [_evaluate_point(point, echo_mode=echo_mode, freshness_mode=freshness_mode) for point in points]
        arms[name] = {
            "echo_mode": echo_mode,
            "freshness_mode": freshness_mode,
            "decisions": decisions,
        }

    baseline = arms["echo_off__freshness_off"]["decisions"]
    echo_treatment = arms["echo_enforce__freshness_off"]["decisions"]
    freshness_treatment = arms["echo_off__freshness_render"]["decisions"]
    observe_echo = [_evaluate_point(point, echo_mode="observe", freshness_mode="off") for point in points]
    observe_freshness = [_evaluate_point(point, echo_mode="off", freshness_mode="observe") for point in points]
    metadata = _id_metadata(points)
    baseline_ids = set(_final_ids(baseline))
    echo_ids = set(_final_ids(echo_treatment))
    freshness_ids = set(_final_ids(freshness_treatment))
    echo_gold_ids = {item_id for item_id, item in metadata.items() if item["gold_label"] == "echo"}
    useful_ids = {item_id for item_id, item in metadata.items() if item["useful"] is True}
    non_echo_ids = set(metadata) - echo_gold_ids
    suppressed_ids = {item_id for decision in echo_treatment for item_id in cast(list[str], decision["suppressed_ids"])}
    resolved = sum(int(decision["source_session_resolved"]) for decision in echo_treatment)
    missing = sum(int(decision["source_session_missing"]) for decision in echo_treatment)
    echo_metrics = {
        "echo_injection_rate": _ratio(len(echo_ids & echo_gold_ids), len(baseline_ids & echo_gold_ids)),
        "echo_suppression_recall": _ratio(len(suppressed_ids & echo_gold_ids), len(baseline_ids & echo_gold_ids)),
        "false_suppression_rate": _ratio(len(suppressed_ids & non_echo_ids), len(baseline_ids & non_echo_ids)),
        "useful_retention": _ratio(len(echo_ids & useful_ids), len(baseline_ids & useful_ids)),
        "source_session_resolution_rate": _ratio(resolved, resolved + missing),
        "empty_packet_delta": sum(not decision["final_ids"] for decision in echo_treatment)
        - sum(not decision["final_ids"] for decision in baseline),
    }
    stable_ids = {
        item_id
        for item_id, item in metadata.items()
        if str(item["canonical_slot"] or "").startswith(("preference.", "identity."))
    }
    freshness_eligible_ids = {
        item_id for decision in freshness_treatment for item_id in cast(list[str], decision["freshness_eligible_ids"])
    }
    token_additions = [
        int(added)
        for decision in freshness_treatment
        for added in cast(dict[str, int], decision["added_tokens_by_id"]).values()
    ]
    budget_by_point = {str(point["point_id"]): int(point["token_budget"]) for point in points}
    cost_gate_points = {str(point["point_id"]) for point in points if point["cost_gate_eligible"] is True}
    added_ratios = [
        sum(int(value) for value in cast(dict[str, int], decision["added_tokens_by_id"]).values())
        / budget_by_point[str(decision["point_id"])]
        for decision in freshness_treatment
        if str(decision["point_id"]) in cost_gate_points and decision["added_tokens_by_id"]
    ]
    freshness_metrics = {
        "maximum_added_tokens": max(token_additions, default=0),
        "p95_added_tokens_to_budget": _percentile(added_ratios, 0.95),
        "stable_fact_retention": _ratio(len(freshness_ids & stable_ids), len(baseline_ids & stable_ids)),
        "false_staleness_rate": _ratio(len(freshness_eligible_ids & stable_ids), len(baseline_ids & stable_ids)),
        "useful_item_retention": _ratio(len(freshness_ids & useful_ids), len(baseline_ids & useful_ids)),
        "packing_changed_points": sum(
            baseline_decision["final_ids"] != treatment_decision["final_ids"]
            for baseline_decision, treatment_decision in zip(baseline, freshness_treatment)
        ),
        "cost_gate_excluded_boundary_points": len(points) - len(cost_gate_points),
    }
    slice_equivalence = {
        cohort: _final_ids(baseline, {cohort}) == _final_ids(echo_treatment, {cohort})
        for cohort in ("cross_session", "historical_and_active", "proper_noun_hard_negative")
    }
    echo_gate = (
        echo_metrics["echo_suppression_recall"] >= 0.80
        and echo_metrics["useful_retention"] >= 0.99
        and echo_metrics["false_suppression_rate"] <= 0.01
        and echo_metrics["source_session_resolution_rate"] >= 0.95
        and echo_metrics["empty_packet_delta"] <= len(points) * 0.01
        and all(slice_equivalence.values())
    )
    freshness_gate = (
        freshness_metrics["maximum_added_tokens"] <= 18
        and freshness_metrics["p95_added_tokens_to_budget"] <= 0.03
        and freshness_metrics["stable_fact_retention"] >= 0.98
        and freshness_metrics["false_staleness_rate"] <= 0.01
        and freshness_metrics["useful_item_retention"] >= 0.99
    )
    shadow_invariants = {
        "echo_observe_equals_off": [item["final_ids"] for item in observe_echo]
        == [item["final_ids"] for item in baseline],
        "freshness_observe_equals_off": [item["final_ids"] for item in observe_freshness]
        == [item["final_ids"] for item in baseline],
    }
    return {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "fixture_schema_version": spec["schema_version"],
        "fixture_sha256": hashlib.sha256(_canonical_json(points).encode("utf-8")).hexdigest(),
        "point_count": len(points),
        "cohort_counts": dict(sorted(Counter(str(point["cohort"]) for point in points).items())),
        "policy_versions": {"echo": ECHO_POLICY_VERSION, "freshness": FRESHNESS_POLICY_VERSION},
        "pipeline_order": list(PIPELINE_ORDER),
        "fixed_inputs": ["candidate_ids", "candidate_text", "reranker_order", "session_signals", "rendering_now"],
        "arms": arms,
        "shadow_invariants": shadow_invariants,
        "echo_metrics": echo_metrics,
        "freshness_metrics": freshness_metrics,
        "slice_equivalence": slice_equivalence,
        "gates": {
            "echo_structural_passed": echo_gate,
            "freshness_structural_passed": freshness_gate,
            "structural_passed": echo_gate and freshness_gate and all(shadow_invariants.values()),
            "online_quality_evaluation": "required_after_deployment",
            "unsafe_obsolete_acceptance": None,
            "verification_action_rate": None,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "tests/fixtures/v0291_injection_replay.json",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--export-expanded-fixture", type=Path)
    args = parser.parse_args(argv)
    spec = load_fixture_spec(args.fixture)
    points = build_fixture(spec)
    report = run_replay(spec, points)
    if args.export_expanded_fixture is not None:
        digest = write_expanded_fixture(points, args.export_expanded_fixture)
        report["expanded_fixture"] = {
            "path": str(args.export_expanded_fixture.resolve()),
            "sha256": digest,
            "point_count": len(points),
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "point_count": len(points),
                "structural_passed": report["gates"]["structural_passed"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report["gates"]["structural_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
