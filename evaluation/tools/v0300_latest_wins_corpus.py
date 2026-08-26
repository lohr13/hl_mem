import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from evaluation.tools.v0300_state_corpus_builder import (
    _coordinate,
    _fingerprint,
    _uniform_real_positions,
    _write_jsonl,
)
from hl_mem.evaluation.state_counterexample_corpus import file_sha256, validate_redacted_seed

ADR_COMMIT = "a1a16c9d315c0360221f30892b566cad2a58d28a"
VALIDATION_QUOTAS = {
    "newer_current_version": 80,
    "newer_rollback": 40,
    "late_arriving_predecessor": 40,
    "duplicate_or_corroborate": 40,
    "cross_coordinate_isolation": 80,
    "non_current_context": 80,
    "fail_closed_boundary": 40,
}
CALIBRATION_QUOTAS = {name: count * 3 // 4 for name, count in VALIDATION_QUOTAS.items()}
_CLAIM_FIELDS = (
    "claim_id coordinate value assertion_kind event_time recorded_at source_authority text source_id".split()
)
_SUBTYPES = {
    "newer_current_version": ("upgrade", "semantic_version", "explicit_probe", "authority_equal"),
    "newer_rollback": ("rollback", "downgrade", "hotfix_revert", "major_revert"),
    "late_arriving_predecessor": ("delayed_recording", "historical_import", "backfill", "clock_order"),
    "duplicate_or_corroborate": ("duplicate", "corroborates"),
    "cross_coordinate_isolation": ("namespace", "subject", "qualifier", "environment"),
    "non_current_context": ("observation", "plan", "quotation", "historical", "negation", "multi_role"),
    "fail_closed_boundary": ("missing_time", "equal_time", "low_authority", "disputed", "bad_chain"),
}
_RELATIONS = {
    "newer_current_version": "supersedes_existing",
    "newer_rollback": "supersedes_existing",
    "late_arriving_predecessor": "historical_predecessor",
    "cross_coordinate_isolation": "compatible",
    "non_current_context": "needs_review",
    "fail_closed_boundary": "needs_review",
}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value is not None else None


def _claim(*parts: Any) -> dict[str, Any]:
    normalized = (
        parts[0],
        dict(parts[1]),
        parts[2],
        "observation",
        _iso(parts[3]),
        _iso(parts[4]),
    ) + parts[5:]
    return dict(zip(_CLAIM_FIELDS, normalized, strict=True))


def _build_case(
    profile: Mapping[str, Any], scenario: str, scenario_index: int, global_index: int, seed: Mapping[str, Any] | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    subtype = _SUBTYPES[scenario][scenario_index % len(_SUBTYPES[scenario])]
    digest = hashlib.sha256(f"{profile['variant_salt']}\0{global_index}".encode()).hexdigest()
    subject = f"component-{digest[:10]}"
    bundle_id = f"{profile['generation_id']}-{scenario.replace('_', '-')}-{scenario_index + 1:03d}"
    base = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=global_index * 3)
    old_time = base
    new_time: datetime | None = base + timedelta(hours=1)
    old_value, new_value = f"v{global_index % 7 + 1}.9.0", f"v{global_index % 7 + 1}.10.0"
    coordinate, incoming_coordinate = _coordinate(subject, "config.version"), None
    old_authority = new_authority = 3
    old_status, chain_acyclic, proof_valid = "active", True, True
    old_source, new_source = f"source-{digest[:8]}-old", f"source-{digest[:8]}-new"
    relation = _RELATIONS.get(scenario, "duplicate" if subtype == "duplicate" else "corroborates")
    if scenario == "newer_rollback":
        old_value, new_value = f"v{global_index % 7 + 3}.4.0", f"v{global_index % 7 + 3}.3.0"
    elif scenario == "late_arriving_predecessor":
        new_time = old_time - timedelta(days=1)
    elif scenario == "duplicate_or_corroborate":
        new_value, new_time = old_value, old_time
        if subtype == "duplicate":
            new_source = old_source
    elif scenario == "cross_coordinate_isolation":
        changes = {
            "namespace": _coordinate(subject, "config.version") | {"namespace": "tenant-b"},
            "subject": _coordinate(f"{subject}-peer", "config.version"),
            "qualifier": _coordinate(subject, "config.version", {"deployment": "green"}),
            "environment": _coordinate(subject, "config.version", {"environment": "staging"}),
        }
        incoming_coordinate = changes[subtype]
    elif scenario == "non_current_context":
        proof_valid = False
    elif scenario == "fail_closed_boundary":
        new_time = None if subtype == "missing_time" else old_time if subtype == "equal_time" else new_time
        new_authority = 2 if subtype == "low_authority" else new_authority
        old_status = "disputed" if subtype == "disputed" else old_status
        chain_acyclic = subtype != "bad_chain"
    incoming_coordinate = incoming_coordinate or coordinate
    family = str(profile["template_family"])
    surface = "状态探针" if "zh" in family else "status probe" if "en" in family else "probe/探针"
    old_text = f"{surface}: {subject} config.version={old_value}"
    new_text = f"{surface}: {subject} config.version={new_value}"
    if scenario == "non_current_context":
        new_text = f"{subtype}: {subject} config.version={new_value}"
    existing = _claim(
        "existing", coordinate, old_value, old_time, base + timedelta(hours=2), old_authority, old_text, old_source
    )
    incoming = _claim(
        "incoming",
        incoming_coordinate,
        new_value,
        new_time,
        base + timedelta(hours=4),
        new_authority,
        new_text,
        new_source,
    )
    incoming["assertion_kind"] = subtype if scenario == "non_current_context" else "observation"
    if subtype == "multi_role":
        incoming.update({"role_count": 2, "payload_count": 2})
    proof = None
    if proof_valid:
        proof = {
            "schema": "status_report_v1",
            "producer": "hl-mem-cli",
            "package": "hl-mem",
            "version": new_value,
            "namespace": incoming_coordinate["namespace"],
            "subject": incoming_coordinate["canonical_subject"],
            "subject_proof": {"alias_table_version": "v1", "owner_id": incoming_coordinate["canonical_subject"]},
            "observed_at": _iso(new_time),
        }
    corpus = {
        "schema_version": 1,
        "bundle_id": bundle_id,
        "profile": profile["name"],
        "scenario": scenario,
        "subtype": subtype,
        "source_kind": "real_deidentified" if seed is not None else "synthetic_adversarial",
        "template_family": family,
        "existing_claim": existing,
        "incoming_claim": incoming,
        "currentness_proof": proof,
        "chain_state": {"current_tip_count": 1, "old_tip_status": old_status, "acyclic": chain_acyclic},
    }
    if seed is not None:
        corpus["context_only"] = {key: seed[key] for key in seed if key != "seed_id"}
    gold = {
        "schema_version": 1,
        "bundle_id": bundle_id,
        "profile": profile["name"],
        "scenario": scenario,
        "subtype": subtype,
        "existing_coordinate": coordinate,
        "incoming_coordinate": incoming_coordinate,
        "expected_temporal_relation": relation,
    }
    return corpus, gold


def _validate_profiles(
    profiles: Sequence[Mapping[str, Any]], seed_pools: Mapping[str, Sequence[Mapping[str, Any]]]
) -> None:
    if [profile.get("name") for profile in profiles] != ["calibration", "validation_a", "validation_b"]:
        raise ValueError("profiles must be calibration, validation_a, validation_b in order")
    for field in ("generation_id", "variant_salt", "template_family"):
        if len({str(profile.get(field) or "") for profile in profiles}) != 3:
            raise ValueError(f"profile {field} values must be non-blank and unique")
    windows = [
        (
            datetime.fromisoformat(str(row["recorded_after_exclusive"]).replace("Z", "+00:00")),
            datetime.fromisoformat(str(row["recorded_before_exclusive"]).replace("Z", "+00:00")),
        )
        for row in profiles
    ]
    if any(start >= end for start, end in windows) or any(
        windows[index][1] > windows[index + 1][0] for index in range(2)
    ):
        raise ValueError("profile context time windows must be ordered and non-overlapping")
    source_sets: list[set[str]] = []
    for profile in profiles:
        name = str(profile["name"])
        quotas = CALIBRATION_QUOTAS if name == "calibration" else VALIDATION_QUOTAS
        seeds = list(seed_pools.get(name, ()))
        if len(seeds) != sum(quotas.values()) * 2 // 5:
            raise ValueError(f"{name} has an invalid redacted real structure count")
        for index, seed in enumerate(seeds):
            validate_redacted_seed(seed, index)
        source_sets.append({str(seed["source_hash"]) for seed in seeds})
    if any(source_sets[left] & source_sets[right] for left in range(3) for right in range(left + 1, 3)):
        raise ValueError("context source pools must be disjoint")


def generate_latest_wins_suite(
    profiles: Sequence[Mapping[str, Any]],
    seed_pools: Mapping[str, Sequence[Mapping[str, Any]]],
    output_dir: str | Path,
) -> dict[str, Any]:
    _validate_profiles(profiles, seed_pools)
    target = Path(output_dir).resolve()
    manifest_profiles: dict[str, Any] = {}
    for profile in profiles:
        name = str(profile["name"])
        quotas = CALIBRATION_QUOTAS if name == "calibration" else VALIDATION_QUOTAS
        total, required_real = sum(quotas.values()), sum(quotas.values()) * 2 // 5
        seeds = list(seed_pools.get(name, ()))
        real_positions = _uniform_real_positions(total, required_real)
        corpus_rows: list[dict[str, Any]] = []
        gold_rows: list[dict[str, Any]] = []
        real_index = global_index = 0
        for scenario, quota in quotas.items():
            for scenario_index in range(quota):
                seed = seeds[real_index] if global_index in real_positions else None
                real_index += seed is not None
                corpus, gold = _build_case(profile, scenario, scenario_index, global_index, seed)
                corpus_rows.append(corpus)
                gold_rows.append(gold)
                global_index += 1
        corpus_path = target / f"v300_latest_wins_{name}_corpus.jsonl"
        gold_path = target / f"v300_latest_wins_{name}_gold.jsonl"
        _write_jsonl(corpus_path, corpus_rows)
        _write_jsonl(gold_path, gold_rows)
        manifest_profiles[name] = {
            **{key: profile[key] for key in profile},
            "bundles": total,
            "quotas": quotas,
            "sources": {"real_deidentified": required_real, "synthetic_adversarial": total - required_real},
            "real_structure_ratio": required_real / total,
            "context_pool_sha256": _fingerprint([seed["source_hash"] for seed in seeds]),
            "files": {
                "corpus": {"path": corpus_path.name, "records": total, "sha256": file_sha256(corpus_path)},
                "gold": {"path": gold_path.name, "records": total, "sha256": file_sha256(gold_path)},
            },
        }
    manifest = {
        "schema_version": 1,
        "protocol": "adr-0004-config-version-latest-wins-corpus-v1",
        "adr_commit": ADR_COMMIT,
        "seal_status": "pending_hermes_review",
        "profiles": manifest_profiles,
    }
    manifest_path = target / "v300_latest_wins_manifest.candidate.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return manifest
