import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from evaluation.tools.v0300_latest_wins_corpus import generate_latest_wins_suite
from hl_mem.evaluation.state_counterexample_corpus import file_sha256

EXPECTED_VALIDATION_QUOTAS = {
    "newer_current_version": 80,
    "newer_rollback": 40,
    "late_arriving_predecessor": 40,
    "duplicate_or_corroborate": 40,
    "cross_coordinate_isolation": 80,
    "non_current_context": 80,
    "fail_closed_boundary": 40,
}
EXPECTED_CALIBRATION_QUOTAS = {name: count * 3 // 4 for name, count in EXPECTED_VALIDATION_QUOTAS.items()}


def _seed(label: str, index: int) -> dict[str, Any]:
    return {
        "seed_id": f"real-{index:03d}",
        "source_hash": hashlib.sha256(f"{label}-source-{index}".encode()).hexdigest(),
        "actor_class": "user" if index % 2 == 0 else "assistant",
        "language_profile": "zh" if label != "validation_b" else "en",
        "length_bucket": "medium",
        "punctuation_profile": {"comma": index % 3, "period": 1, "question": 0},
        "state_signals": ["version"],
        "structure_runs": ["han:8", "ascii:4", "punct:2"],
        "redacted_skeleton": "<HAN:8>服务当前版本是<ASCII:4>",
    }


def _profile_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "calibration",
            "generation_id": "v300-latest-wins-calibration-test",
            "variant_salt": "calibration-test-salt",
            "template_family": "calibration_mixed_v1",
            "recorded_after_exclusive": "2026-07-20T23:59:59Z",
            "recorded_before_exclusive": "2026-08-09T00:00:00Z",
        },
        {
            "name": "validation_a",
            "generation_id": "v300-latest-wins-validation-a-test",
            "variant_salt": "validation-a-test-salt",
            "template_family": "validation_zh_ops_v1",
            "recorded_after_exclusive": "2026-08-09T00:00:00Z",
            "recorded_before_exclusive": "2026-08-17T00:00:00Z",
        },
        {
            "name": "validation_b",
            "generation_id": "v300-latest-wins-validation-b-test",
            "variant_salt": "validation-b-test-salt",
            "template_family": "validation_en_release_v1",
            "recorded_after_exclusive": "2026-08-17T00:00:00Z",
            "recorded_before_exclusive": "2026-08-27T00:00:00Z",
        },
    ]


def _seed_pools() -> dict[str, list[dict[str, Any]]]:
    return {
        "calibration": [_seed("calibration", index) for index in range(120)],
        "validation_a": [_seed("validation_a", index) for index in range(160)],
        "validation_b": [_seed("validation_b", index) for index in range(160)],
    }


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _generate(tmp_path: Path) -> tuple[dict[str, Any], Path, Path]:
    output = tmp_path / "datasets"
    review = tmp_path / "review"
    return generate_latest_wins_suite(_profile_specs(), _seed_pools(), output), output, review


def test_latest_wins_suite_freezes_three_profiles_with_exact_adr_quotas(tmp_path: Path) -> None:
    manifest, output, _review = _generate(tmp_path)

    assert manifest["protocol"] == "adr-0004-config-version-latest-wins-corpus-v1"
    assert manifest["adr_commit"] == "a1a16c9d315c0360221f30892b566cad2a58d28a"
    assert manifest["seal_status"] == "pending_hermes_review"
    assert set(manifest["profiles"]) == {"calibration", "validation_a", "validation_b"}
    assert manifest["profiles"]["calibration"]["template_family"] == "calibration_mixed_v1"
    assert manifest["profiles"]["validation_a"]["template_family"] == "validation_zh_ops_v1"
    assert manifest["profiles"]["validation_b"]["template_family"] == "validation_en_release_v1"
    assert manifest["profiles"]["calibration"]["bundles"] == 300
    assert manifest["profiles"]["calibration"]["quotas"] == EXPECTED_CALIBRATION_QUOTAS
    assert manifest["profiles"]["validation_a"]["bundles"] == 400
    assert manifest["profiles"]["validation_a"]["quotas"] == EXPECTED_VALIDATION_QUOTAS
    assert manifest["profiles"]["validation_b"]["bundles"] == 400
    assert manifest["profiles"]["validation_b"]["quotas"] == EXPECTED_VALIDATION_QUOTAS
    for profile, bundles, real_count in (
        ("calibration", 300, 120),
        ("validation_a", 400, 160),
        ("validation_b", 400, 160),
    ):
        assert manifest["profiles"][profile]["sources"] == {
            "real_deidentified": real_count,
            "synthetic_adversarial": bundles - real_count,
        }
        assert manifest["profiles"][profile]["real_structure_ratio"] == 0.4
        assert len(_jsonl(output / f"v300_latest_wins_{profile}_corpus.jsonl")) == bundles
        assert len(_jsonl(output / f"v300_latest_wins_{profile}_gold.jsonl")) == bundles


def test_gold_is_structure_only_and_covers_all_six_temporal_relations(tmp_path: Path) -> None:
    _manifest, output, _review = _generate(tmp_path)
    gold_rows = [
        row
        for profile in ("calibration", "validation_a", "validation_b")
        for row in _jsonl(output / f"v300_latest_wins_{profile}_gold.jsonl")
    ]

    expected_keys = {
        "schema_version",
        "bundle_id",
        "profile",
        "scenario",
        "subtype",
        "existing_coordinate",
        "incoming_coordinate",
        "expected_temporal_relation",
    }
    assert all(set(row) == expected_keys for row in gold_rows)
    serialized = json.dumps(gold_rows, ensure_ascii=False)
    assert all(forbidden not in serialized for forbidden in ('"text"', '"reason"', '"provenance"', '"context_only"'))
    assert set(Counter(row["expected_temporal_relation"] for row in gold_rows)) == {
        "duplicate",
        "corroborates",
        "supersedes_existing",
        "historical_predecessor",
        "compatible",
        "needs_review",
    }


def test_structural_cases_encode_direction_isolation_and_fail_closed_inputs(tmp_path: Path) -> None:
    _manifest, output, _review = _generate(tmp_path)
    rows = _jsonl(output / "v300_latest_wins_validation_a_corpus.jsonl")
    gold = {row["bundle_id"]: row for row in _jsonl(output / "v300_latest_wins_validation_a_gold.jsonl")}

    rollback = next(row for row in rows if row["scenario"] == "newer_rollback")
    assert rollback["incoming_claim"]["event_time"] > rollback["existing_claim"]["event_time"]
    assert gold[rollback["bundle_id"]]["expected_temporal_relation"] == "supersedes_existing"
    predecessor = next(row for row in rows if row["scenario"] == "late_arriving_predecessor")
    assert predecessor["incoming_claim"]["event_time"] < predecessor["existing_claim"]["event_time"]
    assert predecessor["incoming_claim"]["recorded_at"] > predecessor["existing_claim"]["recorded_at"]
    assert gold[predecessor["bundle_id"]]["expected_temporal_relation"] == "historical_predecessor"
    duplicate = next(
        row for row in rows if row["scenario"] == "duplicate_or_corroborate" and row["subtype"] == "duplicate"
    )
    corroborate = next(
        row for row in rows if row["scenario"] == "duplicate_or_corroborate" and row["subtype"] == "corroborates"
    )
    assert duplicate["existing_claim"]["source_id"] == duplicate["incoming_claim"]["source_id"]
    assert corroborate["existing_claim"]["source_id"] != corroborate["incoming_claim"]["source_id"]
    isolated = next(row for row in rows if row["scenario"] == "cross_coordinate_isolation")
    assert isolated["existing_claim"]["coordinate"] != isolated["incoming_claim"]["coordinate"]
    assert gold[isolated["bundle_id"]]["expected_temporal_relation"] == "compatible"
    gray = next(row for row in rows if row["scenario"] == "non_current_context")
    assert gray["currentness_proof"] is None
    assert gold[gray["bundle_id"]]["expected_temporal_relation"] == "needs_review"


def test_generation_is_profile_independent_and_byte_deterministic(tmp_path: Path) -> None:
    first, first_output, _first_review = _generate(tmp_path / "first")
    second, second_output, _second_review = _generate(tmp_path / "second")

    assert first == second
    id_groups = [
        {row["bundle_id"] for row in _jsonl(first_output / f"v300_latest_wins_{name}_corpus.jsonl")}
        for name in ("calibration", "validation_a", "validation_b")
    ]
    assert all(not id_groups[left] & id_groups[right] for left in range(3) for right in range(left + 1, 3))
    assert (first_output / "v300_latest_wins_manifest.candidate.json").read_bytes() == (
        second_output / "v300_latest_wins_manifest.candidate.json"
    ).read_bytes()


def test_manifest_hashes_all_dataset_artifacts(tmp_path: Path) -> None:
    manifest, output, _review = _generate(tmp_path)

    for profile in manifest["profiles"].values():
        for entry in profile["files"].values():
            assert file_sha256(output / entry["path"]) == entry["sha256"]


def test_generator_rejects_non_independent_profiles_or_context_pools(tmp_path: Path) -> None:
    specs = _profile_specs()
    specs[1]["variant_salt"] = specs[0]["variant_salt"]
    with pytest.raises(ValueError, match="variant_salt"):
        generate_latest_wins_suite(specs, _seed_pools(), tmp_path / "datasets")

    pools = _seed_pools()
    pools["validation_b"][0]["source_hash"] = pools["validation_a"][0]["source_hash"]
    with pytest.raises(ValueError, match="context source pools must be disjoint"):
        generate_latest_wins_suite(_profile_specs(), pools, tmp_path / "datasets2")


@pytest.mark.parametrize("field", ["generation_id", "template_family"])
def test_generator_rejects_duplicate_generation_identity_fields(tmp_path: Path, field: str) -> None:
    specs = _profile_specs()
    specs[1][field] = specs[0][field]

    with pytest.raises(ValueError, match=field):
        generate_latest_wins_suite(specs, _seed_pools(), tmp_path / "datasets")


def test_generator_rejects_a_real_structure_ratio_below_forty_percent(tmp_path: Path) -> None:
    pools = _seed_pools()
    pools["validation_a"].pop()

    with pytest.raises(ValueError, match="validation_a has an invalid"):
        generate_latest_wins_suite(_profile_specs(), pools, tmp_path / "datasets")


def test_validation_relation_counts_and_bundle_identifiers_are_frozen(tmp_path: Path) -> None:
    _manifest, output, _review = _generate(tmp_path)

    all_ids: list[str] = []
    for profile in ("validation_a", "validation_b"):
        corpus = _jsonl(output / f"v300_latest_wins_{profile}_corpus.jsonl")
        gold = _jsonl(output / f"v300_latest_wins_{profile}_gold.jsonl")
        assert Counter(row["expected_temporal_relation"] for row in gold) == {
            "supersedes_existing": 120,
            "historical_predecessor": 40,
            "duplicate": 20,
            "corroborates": 20,
            "compatible": 80,
            "needs_review": 120,
        }
        assert Counter(row["scenario"] for row in corpus) == EXPECTED_VALIDATION_QUOTAS
        assert {row["bundle_id"] for row in corpus} == {row["bundle_id"] for row in gold}
        all_ids.extend(row["bundle_id"] for row in corpus)
    assert len(all_ids) == len(set(all_ids)) == 800


def test_currentness_proof_uses_the_fixed_status_report_contract(tmp_path: Path) -> None:
    _manifest, output, _review = _generate(tmp_path)
    corpus = [
        row
        for profile in ("calibration", "validation_a", "validation_b")
        for row in _jsonl(output / f"v300_latest_wins_{profile}_corpus.jsonl")
    ]
    proof_rows = [row for row in corpus if row["currentness_proof"] is not None]

    assert len(proof_rows) == 880
    for row in proof_rows:
        proof = row["currentness_proof"]
        incoming = row["incoming_claim"]
        coordinate = incoming["coordinate"]
        assert set(proof) == {
            "schema_version",
            "producer_contract",
            "package",
            "runtime_version",
            "namespace",
            "subject_proof",
            "observed_at",
        }
        assert proof["schema_version"] == "status_report_v1"
        assert proof["producer_contract"] == "hl_mem.report-version-v1"
        assert proof["package"] == "hl_mem"
        assert proof["runtime_version"] == incoming["value"]
        assert proof["namespace"] == coordinate["namespace"]
        assert proof["observed_at"] == incoming["event_time"]
        assert proof["subject_proof"] == {
            "canonical_entity_id": coordinate["canonical_subject"],
            "alias_version": 1,
        }


def test_non_current_context_is_encoded_in_inputs_without_gold_side_channels(tmp_path: Path) -> None:
    _manifest, output, _review = _generate(tmp_path)
    corpus = _jsonl(output / "v300_latest_wins_validation_b_corpus.jsonl")
    rows = [row for row in corpus if row["scenario"] == "non_current_context"]

    assert Counter(row["subtype"] for row in rows) == {
        "observation": 14,
        "plan": 14,
        "quotation": 13,
        "historical": 13,
        "negation": 13,
        "multi_role": 13,
    }
    assert all(row["currentness_proof"] is None for row in rows)
    assert all(row["incoming_claim"]["assertion_kind"] == row["subtype"] for row in rows)
    multi_role = next(row for row in rows if row["subtype"] == "multi_role")
    assert multi_role["incoming_claim"]["role_count"] == 2
    assert multi_role["incoming_claim"]["payload_count"] == 2


def test_fail_closed_stratum_contains_each_registered_structural_defect(tmp_path: Path) -> None:
    _manifest, output, _review = _generate(tmp_path)
    rows = {
        row["subtype"]: row
        for row in _jsonl(output / "v300_latest_wins_validation_a_corpus.jsonl")
        if row["scenario"] == "fail_closed_boundary"
    }

    assert set(rows) == {"missing_time", "equal_time", "low_authority", "disputed", "bad_chain"}
    assert rows["missing_time"]["incoming_claim"]["event_time"] is None
    assert rows["equal_time"]["incoming_claim"]["event_time"] == rows["equal_time"]["existing_claim"]["event_time"]
    assert (
        rows["low_authority"]["incoming_claim"]["source_authority"]
        < rows["low_authority"]["existing_claim"]["source_authority"]
    )
    assert rows["disputed"]["chain_state"]["old_tip_status"] == "disputed"
    assert rows["bad_chain"]["chain_state"]["acyclic"] is False


def test_real_context_is_irreversible_structure_and_never_fact_evidence(tmp_path: Path) -> None:
    _manifest, output, _review = _generate(tmp_path)
    rows = _jsonl(output / "v300_latest_wins_calibration_corpus.jsonl")
    real_rows = [row for row in rows if row["source_kind"] == "real_deidentified"]

    assert len(real_rows) == 120
    assert all("seed_id" not in row["context_only"] for row in real_rows)
    assert all("redacted_skeleton" in row["context_only"] for row in real_rows)
    assert all(row["existing_claim"]["source_id"] not in row["context_only"]["redacted_skeleton"] for row in real_rows)
