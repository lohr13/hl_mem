#!/usr/bin/env python
"""Merge read-only Volcano evidence into the private frozen v0.30 manifests."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from benchmarks.archive.v030.v030_corpus import build_manifest, load_manifest, write_manifest

E1_DEFAULT_CUTOFF = "2026-08-21T00:00:00+00:00"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _require_rows(evidence: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    rows = evidence.get(key)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"remote evidence {key} must be a list of objects")
    return rows


def _claim_index(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result = {str(row.get("id") or ""): row for row in rows}
    if "" in result or len(result) != len(rows):
        raise ValueError("remote evidence claim IDs must be non-empty and unique")
    return result


def _parse_json_field(row: Mapping[str, Any], key: str) -> Any:
    value = row.get(key)
    if value is None or not isinstance(value, str):
        return value
    return json.loads(value)


def _claim_snapshot(row: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = copy.deepcopy(dict(row))
    snapshot["value"] = _parse_json_field(row, "value_json")
    snapshot["qualifiers"] = _parse_json_field(row, "qualifiers_json")
    snapshot["entities"] = _parse_json_field(row, "entities_json")
    return snapshot


def _replace_volcano_snapshot(
    manifest: Mapping[str, Any], *, source_id: str, evidence_sha256: str
) -> list[dict[str, Any]]:
    snapshots = [
        copy.deepcopy(dict(item))
        for item in manifest["source_snapshots"]
        if not str(item.get("source_id") or "").startswith("volcano")
    ]
    snapshots.append({"source_id": source_id, "sha256": evidence_sha256, "reconstructable": True})
    return snapshots


def _build_e1(
    current: Mapping[str, Any], evidence: Mapping[str, Any], evidence_sha256: str, cutoff_text: str
) -> dict[str, Any]:
    raw_cases = _require_rows(evidence, "cases")
    raw_claims = _require_rows(evidence, "claims")
    candidate_members = _require_rows(evidence, "candidate_members")
    case_candidates = _require_rows(evidence, "case_candidates")
    cutoff = datetime.fromisoformat(cutoff_text)
    included = sorted(
        (row for row in raw_cases if datetime.fromisoformat(str(row.get("resolved_at") or "")) < cutoff),
        key=lambda row: str(row["id"]),
    )
    excluded = sorted((row for row in raw_cases if row not in included), key=lambda row: str(row["id"]))
    if len(raw_cases) != 13 or len(included) != 11 or len(excluded) != 2:
        raise ValueError("E1 remote evidence must select exactly 11 cases from the 13-case superset")
    if not {str(row.get("decision") or "") for row in included} <= {"keep_left", "keep_right"}:
        raise ValueError("E1 remote evidence contains an unsupported decision")
    claims = _claim_index(raw_claims)
    cases = [copy.deepcopy(dict(case)) for case in current["cases"] if case.get("source") != "volcano"]
    for row in included:
        case_id = str(row["id"])
        left_id = str(row.get("left_claim_id") or "")
        right_id = str(row.get("right_claim_id") or "")
        if left_id not in claims or right_id not in claims:
            raise ValueError(f"E1 case {case_id} references a missing claim")
        structural_case = {
            key: copy.deepcopy(value)
            for key, value in row.items()
            if key not in {"status", "decision", "rationale", "confidence", "resolved_at"}
        }
        cases.append(
            {
                "case_id": f"volcano:{case_id}",
                "source": "volcano",
                "category": "conflict",
                "input": {
                    "case": structural_case,
                    "claims": [_claim_snapshot(claims[left_id]), _claim_snapshot(claims[right_id])],
                    "candidate_members": [
                        copy.deepcopy(item) for item in candidate_members if str(item.get("case_id")) == case_id
                    ],
                    "candidates": [
                        copy.deepcopy(item) for item in case_candidates if str(item.get("case_id")) == case_id
                    ],
                    "evidence_refs": {},
                },
                "gold": {
                    "decision": row["decision"],
                    "rationale": row.get("rationale"),
                    "confidence": row.get("confidence"),
                    "resolved_at": row.get("resolved_at"),
                    "gold_invariant_status": "hermes_verified",
                },
            }
        )
    audit = copy.deepcopy(dict(current["source_audit"]))
    audit["volcano_raw_case_ids"] = [str(row["id"]) for row in included]
    audit["volcano_decision_distribution"] = dict(sorted(Counter(str(row["decision"]) for row in included).items()))
    audit["volcano_selection"] = {
        "candidate_count": len(raw_cases),
        "cutoff": cutoff_text,
        "evidence_sha256": evidence_sha256,
        "excluded_count": len(excluded),
        "included_count": len(included),
        "rule": "resolved_at_before_cutoff_exact_11_case_batch",
    }
    reason = f"resolved_at_on_or_after_{cutoff_text}_adjacent_date_outside_exact_11_case_batch"
    audit["excluded_with_reason"] = [
        {"case_id": str(row["id"]), "resolved_at": row.get("resolved_at"), "reason": reason} for row in excluded
    ]
    return build_manifest(
        "E1",
        cases,
        source_snapshots=_replace_volcano_snapshot(
            current, source_id="volcano_remote_evidence_e1", evidence_sha256=evidence_sha256
        ),
        source_audit=audit,
    )


def _build_e2(current: Mapping[str, Any], evidence: Mapping[str, Any], evidence_sha256: str) -> dict[str, Any]:
    raw_pairs = _require_rows(evidence, "pairs")
    raw_claims = _require_rows(evidence, "claims")
    if len(raw_pairs) != 15 or any(row.get("decision") != "equivalent" for row in raw_pairs):
        raise ValueError("E2 remote evidence must contain exactly 15 equivalent pairs")
    claims = _claim_index(raw_claims)
    cases = [copy.deepcopy(dict(case)) for case in current["cases"] if case.get("source") != "volcano"]
    gold_fields = {"decision", "judge_confidence", "judge_model", "judge_reason", "reviewed_at"}
    for row in sorted(raw_pairs, key=lambda item: str(item["id"])):
        pair_id = str(row["id"])
        left_id = str(row.get("left_claim_id") or "")
        right_id = str(row.get("right_claim_id") or "")
        if left_id not in claims or right_id not in claims:
            raise ValueError(f"E2 pair {pair_id} references a missing claim")
        pair_input = {key: copy.deepcopy(value) for key, value in row.items() if key not in gold_fields}
        cases.append(
            {
                "case_id": f"volcano:dedup:{pair_id}",
                "source": "volcano",
                "category": "equivalent",
                "input": {
                    **pair_input,
                    "pair_id": pair_id,
                    "claims": [_claim_snapshot(claims[left_id]), _claim_snapshot(claims[right_id])],
                },
                "gold": {
                    "decision": "equivalent",
                    "judge_confidence": row.get("judge_confidence"),
                    "judge_model": row.get("judge_model"),
                    "judge_reason": row.get("judge_reason"),
                    "reviewed_at": row.get("reviewed_at"),
                    "gold_status": "historical_decision_requires_blind_recheck",
                },
            }
        )
    audit = copy.deepcopy(dict(current["source_audit"]))
    audit["volcano_pair_columns"] = list(raw_pairs[0])
    audit["volcano_pair_ids"] = sorted(str(row["id"]) for row in raw_pairs)
    audit["volcano_claim_ids"] = sorted(claims)
    audit["volcano_evidence_sha256"] = evidence_sha256
    return build_manifest(
        "E2",
        cases,
        source_snapshots=_replace_volcano_snapshot(
            current, source_id="volcano_remote_evidence_e2", evidence_sha256=evidence_sha256
        ),
        source_audit=audit,
    )


def refreeze_remote_evidence(
    manifest_dir: str | Path,
    e1_evidence_path: str | Path,
    e2_evidence_path: str | Path,
    *,
    e1_cutoff: str = E1_DEFAULT_CUTOFF,
) -> dict[str, Any]:
    """Replace Volcano placeholders and deterministically reseal E1/E2 manifests."""

    root = Path(manifest_dir)
    e1_path = Path(e1_evidence_path)
    e2_path = Path(e2_evidence_path)
    e1 = _build_e1(load_manifest(root / "e1.json"), _load_object(e1_path), _file_sha256(e1_path), e1_cutoff)
    e2 = _build_e2(load_manifest(root / "e2.json"), _load_object(e2_path), _file_sha256(e2_path))
    temporary_paths = {"E1": root / "e1.json.tmp", "E2": root / "e2.json.tmp"}
    for experiment, manifest in (("E1", e1), ("E2", e2)):
        write_manifest(temporary_paths[experiment], manifest)
    temporary_paths["E1"].replace(root / "e1.json")
    temporary_paths["E2"].replace(root / "e2.json")
    return {
        "case_counts": {"E1": len(e1["cases"]), "E2": len(e2["cases"])},
        "manifest_sha256": {"E1": e1["manifest_sha256"], "E2": e2["manifest_sha256"]},
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest-dir", type=Path, required=True)
    parser.add_argument("--e1-evidence", type=Path, required=True)
    parser.add_argument("--e2-evidence", type=Path, required=True)
    parser.add_argument("--e1-cutoff", default=E1_DEFAULT_CUTOFF)
    arguments = parser.parse_args(argv)
    result = refreeze_remote_evidence(
        arguments.manifest_dir,
        arguments.e1_evidence,
        arguments.e2_evidence,
        e1_cutoff=arguments.e1_cutoff,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
