"""Materialize exact four-arm Context Packet bodies for behavioral samples."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from scripts.run_v0291_injection_replay import ARM_SPECS, evaluate_point


def materialize_behavioral_arms(
    manifest: Mapping[str, Any],
    samples: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Run production echo/freshness/packing policy for every behavioral point and arm."""

    assignments: list[dict[str, Any]] = []
    for sample in samples:
        replay_point = _behavioral_replay_point(manifest, sample)
        for arm_name, echo_mode, freshness_mode in ARM_SPECS:
            decision = evaluate_point(replay_point, echo_mode=echo_mode, freshness_mode=freshness_mode)
            assignments.append(
                {
                    "opaque_sample_id": sample["opaque_sample_id"],
                    "scenario_family_id": sample["scenario_family_id"],
                    "cohort": sample["cohort"],
                    "arm_name": arm_name,
                    "echo_mode": echo_mode,
                    "freshness_mode": freshness_mode,
                    **{key: value for key, value in decision.items() if key not in {"point_id", "cohort"}},
                }
            )
    return assignments


def _behavioral_replay_point(
    manifest: Mapping[str, Any],
    sample: Mapping[str, Any],
) -> dict[str, Any]:
    reference = sample["reference"]
    sample_id = str(sample["opaque_sample_id"])
    return {
        "point_id": sample_id,
        "cohort": str(sample["cohort"]),
        "query": str(sample["user_prompt"]),
        "namespace": str(manifest["namespace"]),
        "session_id": str(manifest["session_id"]),
        "delivery_purpose": str(sample["delivery_purpose"]),
        "intent": str(sample["intent"]),
        "as_of": sample.get("as_of"),
        "known_as_of": sample.get("known_as_of"),
        "rendering_now": str(manifest["rendering_now"]),
        "token_budget": int(sample.get("token_budget", manifest["default_token_budget"])),
        "cost_gate_eligible": sample.get("boundary_kind") != "packing_boundary",
        "candidates": [
            {
                "id": f"memory-{sample_id}",
                "type": "claim",
                "text": str(reference["text"]),
                "recorded_from": reference.get("recorded_from"),
                "canonical_slot": reference.get("canonical_slot"),
                "canonical_attribute": reference.get("canonical_attribute"),
                "topic_tags": list(reference.get("topic_tags") or []),
                "gold_label": "useful_cross_session",
                "useful": True,
                "signal": {
                    "source_session_resolved": True,
                    "matching_session_recorded_at": None,
                },
                "rerank_position": 0,
            }
        ],
    }
