from __future__ import annotations

import json
from pathlib import Path

from evaluation.v0291_behavioral.manifest import (
    expand_behavioral_samples,
    load_behavioral_manifest,
)
from evaluation.v0291_behavioral.packet import materialize_behavioral_arms

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "tests/fixtures/v0291_freshness_behavioral.json"


def test_behavioral_packet_materialization_exports_every_exact_arm_body() -> None:
    manifest = load_behavioral_manifest(MANIFEST_PATH)
    samples = expand_behavioral_samples(manifest)
    assignments = materialize_behavioral_arms(manifest, samples)

    assert len(assignments) == 320
    assert {assignment["arm_name"] for assignment in assignments} == {
        "echo_off__freshness_off",
        "echo_enforce__freshness_off",
        "echo_off__freshness_render",
        "echo_enforce__freshness_render",
    }
    for assignment in assignments:
        packet = assignment["context_packet"]
        assert json.loads(assignment["context_packet_text"]) == packet
        assert packet["query_id"] == assignment["opaque_sample_id"]
        assert [item["id"] for item in packet["items"]] == assignment["final_ids"]
        assert assignment["arm_name"] not in assignment["context_packet_text"]
        assert assignment["cohort"] not in assignment["context_packet_text"]


def test_behavioral_packets_are_deterministic_and_change_only_when_arm_output_changes() -> None:
    manifest = load_behavioral_manifest(MANIFEST_PATH)
    samples = expand_behavioral_samples(manifest)
    first = materialize_behavioral_arms(manifest, samples)
    second = materialize_behavioral_arms(manifest, samples)

    assert first == second
    by_sample: dict[str, list[dict[str, object]]] = {}
    for assignment in first:
        by_sample.setdefault(assignment["opaque_sample_id"], []).append(assignment)

    incident = next(sample for sample in samples if sample["cohort"] == "incident")
    stable = next(sample for sample in samples if sample["cohort"] == "stable_negative")
    incident_packets = {item["context_packet_text"] for item in by_sample[incident["opaque_sample_id"]]}
    stable_packets = {item["context_packet_text"] for item in by_sample[stable["opaque_sample_id"]]}
    assert len(incident_packets) == 2
    assert len(stable_packets) == 1
