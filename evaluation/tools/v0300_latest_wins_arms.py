"""One-shot frozen A/B replay for ADR-0004 validation corpora."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from hl_mem.domain.claims.state_coordinates import StateCoordinate
from hl_mem.state_latest_wins import (
    CurrentnessProof,
    CurrentTipState,
    VersionClaim,
    resolve_latest_wins,
)

RULE_COMMIT = "c1a1774fb7c98086f6ff2335bf5abb04dff44d6f"
MANIFEST_SHA256 = "25be3ca732d5def5feab03e100012bdac8c54918eec1852ad047cb2eb768cca8"
HERMES_VALIDATOR_SHA256 = "746e0e43bcaddae4ad2e182426bbc42ecae8b6976d389742a89cddcc0576e64a"
ACTION_RELATIONS = frozenset({"duplicate", "corroborates", "supersedes_existing", "historical_predecessor"})
EDGE_RELATIONS = frozenset({"supersedes_existing", "historical_predecessor"})
Rows = list[dict[str, Any]]
Gold = dict[str, dict[str, Any]]


def _claim(raw: dict[str, Any]) -> VersionClaim:
    coordinate = raw["coordinate"]
    qualifiers = coordinate.get("coordinate_qualifiers", {})
    state_coordinate = StateCoordinate(
        coordinate["namespace"], coordinate["canonical_subject"], coordinate["canonical_slot"], qualifiers
    )
    return VersionClaim(
        claim_id=raw["claim_id"],
        coordinate=state_coordinate,
        value=raw["value"],
        assertion_kind=raw.get("assertion_kind", "observation"),
        event_time=raw.get("event_time"),
        source_authority=raw["source_authority"],
        evidence_id=raw.get("source_id"),
        event_time_trusted=raw.get("event_time_trusted", True),
        evidence_grounded=raw.get("evidence_grounded", True),
        polarity="negative" if raw.get("negation") else raw.get("polarity", "positive"),
        semantic_anchors=frozenset(tuple(item) for item in raw.get("semantic_anchors", [])),
        role_count=raw.get("role_count", 1),
        payload_count=raw.get("payload_count", 1),
        atomic_value=raw.get("atomic_value", True),
    )


def _proof(raw: dict[str, Any] | None) -> CurrentnessProof | None:
    if raw is None:
        return None
    subject = raw["subject_proof"]
    return CurrentnessProof(
        schema_version=raw["schema_version"],
        producer_contract=raw["producer_contract"],
        package=raw["package"],
        runtime_version=raw["runtime_version"],
        namespace=raw["namespace"],
        canonical_entity_id=subject["canonical_entity_id"],
        alias_version=subject["alias_version"],
        observed_at=raw.get("observed_at"),
        producer_and_owner_verified=raw.get("producer_and_owner_verified", True),
    )


def run_arm(corpus: Rows, gold: Gold, arm: str) -> Rows:
    records: Rows = []
    if arm not in {"A", "B"}:
        raise ValueError(f"unknown arm: {arm}")
    if {row["bundle_id"] for row in corpus} != set(gold):
        raise ValueError("corpus/gold bundle ids differ")
    for bundle in corpus:
        bundle_id = bundle["bundle_id"]
        expected = gold[bundle_id]["expected_temporal_relation"]
        chain = bundle["chain_state"]
        resolved = None
        if arm == "B":
            old, new = _claim(bundle["existing_claim"]), _claim(bundle["incoming_claim"])
            snapshot = chain.get("local_snapshot_matches", True)
            tip = CurrentTipState(chain["current_tip_count"], chain["old_tip_status"], chain["acyclic"], snapshot)
            resolved = resolve_latest_wins(old, new, _proof(bundle.get("currentness_proof")), tip)
        actual = "compatible" if resolved is None else resolved.relation
        records.append(
            {
                "arm": arm,
                "profile": bundle["profile"],
                "bundle_id": bundle_id,
                "scenario": bundle["scenario"],
                "source_kind": bundle["source_kind"],
                "expected_temporal_relation": expected,
                "actual_relation": actual,
                "rule_id": "state-latest-wins-off" if resolved is None else resolved.rule_id,
                "reason": "mode_off" if resolved is None else resolved.reason,
            }
        )
    return records


def _ratio(numerator: int, denominator: int) -> dict[str, int | float]:
    result: dict[str, int | float] = {"numerator": numerator, "denominator": denominator}
    if denominator:
        result["value"] = numerator / denominator
    return result


def summarize(records: Rows) -> dict[str, Any]:
    edges = [row for row in records if row["actual_relation"] in EDGE_RELATIONS]
    counterexamples = [row for row in records if row["expected_temporal_relation"] not in ACTION_RELATIONS]
    cross = [row for row in records if row["scenario"] == "cross_coordinate_isolation"]
    historical = [row for row in records if row["expected_temporal_relation"] == "historical_predecessor"]
    equivalent = [row for row in records if row["expected_temporal_relation"] in {"duplicate", "corroborates"}]
    eligible = [row for row in records if row["expected_temporal_relation"] in ACTION_RELATIONS]
    real_stale = [
        row
        for row in records
        if row["source_kind"] == "real_deidentified" and row["expected_temporal_relation"] in EDGE_RELATIONS
    ]
    return {
        "records": len(records),
        "automatic_edge_precision": _ratio(
            sum(row["actual_relation"] == row["expected_temporal_relation"] for row in edges), len(edges)
        ),
        "counterexample_false_supersede": _ratio(
            sum(row["actual_relation"] == "supersedes_existing" for row in counterexamples), len(counterexamples)
        ),
        "cross_coordinate_auto_action": _ratio(
            sum(row["actual_relation"] in ACTION_RELATIONS for row in cross), len(cross)
        ),
        "historical_direction_accuracy": _ratio(
            sum(row["actual_relation"] == "historical_predecessor" for row in historical), len(historical)
        ),
        "equivalent_revision_growth_zero": _ratio(
            sum(row["actual_relation"] == row["expected_temporal_relation"] for row in equivalent), len(equivalent)
        ),
        "eligible_recall": _ratio(
            sum(row["actual_relation"] == row["expected_temporal_relation"] for row in eligible), len(eligible)
        ),
        "real_cohort_stale_reduction": _ratio(
            sum(row["actual_relation"] == row["expected_temporal_relation"] for row in real_stale), len(real_stale)
        ),
    }


def _digest(records: Rows) -> str:
    payload = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def replay_arm(corpus: Rows, gold: Gold, arm: str) -> tuple[Rows, list[str]]:
    replays = [run_arm(corpus, gold, arm) for _ in range(3)]
    digests = [_digest(records) for records in replays]
    if len(set(digests)) != 1:
        raise RuntimeError(f"arm {arm} deterministic replay mismatch")
    return replays[0], digests


def _jsonl(path: Path) -> Rows:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _verify(repo: Path, manifest_path: Path) -> dict[str, Any]:
    if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != MANIFEST_SHA256:
        raise RuntimeError("frozen manifest hash mismatch")
    changed = subprocess.run(["git", "diff", "--quiet", RULE_COMMIT, "--", "src"], cwd=repo, check=False).returncode
    if changed:
        raise RuntimeError("src differs from frozen rule commit")
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name in ("validation_a", "validation_b"):
        for item in manifest["profiles"][name]["files"].values():
            path = manifest_path.parent / item["path"]
            if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                raise RuntimeError(f"frozen dataset hash mismatch: {item['path']}")
    return manifest


def main() -> None:
    repo = Path(__file__).resolve().parents[2]
    dataset_dir = repo / "evaluation" / "datasets"
    output_dir = dataset_dir / "v300_arms"
    targets = [output_dir / name for name in ("arm_A.json", "arm_B.json", "report.json")]
    if any(path.exists() for path in targets):
        raise RuntimeError("frozen arm output already exists")
    manifest_path = dataset_dir / "v300_latest_wins_manifest.candidate.json"
    manifest = _verify(repo, manifest_path)
    corpus: Rows = []
    gold: Gold = {}
    for profile in ("validation_a", "validation_b"):
        files = manifest["profiles"][profile]["files"]
        corpus.extend(_jsonl(dataset_dir / files["corpus"]["path"]))
        rows = _jsonl(dataset_dir / files["gold"]["path"])
        gold.update({row["bundle_id"]: row for row in rows})
    arm_a, replay_a = replay_arm(corpus, gold, "A")
    arm_b, replay_b = replay_arm(corpus, gold, "B")
    report = {
        "rule_commit": RULE_COMMIT,
        "manifest_sha256": MANIFEST_SHA256,
        "hermes_validator_sha256": HERMES_VALIDATOR_SHA256,
        "arms": {"A": summarize(arm_a), "B": summarize(arm_b)},
        "deterministic_replay": {"A": replay_a, "B": replay_b},
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    for path, payload in zip(targets, (arm_a, arm_b, report), strict=True):
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
