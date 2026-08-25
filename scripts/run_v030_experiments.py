#!/usr/bin/env python
"""Validate and orchestrate the frozen v0.30 experiment manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import tomllib
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from hl_mem.evaluation.v030_corpus import EXPERIMENTS, load_manifest, validate_manifest


def validate_manifest_directory(manifest_dir: str | Path) -> dict[str, object]:
    """Authenticate E1-E6 and summarize the frozen manifest set."""

    root = Path(manifest_dir)
    summaries: dict[str, dict[str, object]] = {}
    digests: dict[str, str] = {}
    for experiment in sorted(EXPERIMENTS):
        manifest = load_manifest(root / f"{experiment.lower()}.json")
        if manifest["experiment"] != experiment:
            raise ValueError(f"{experiment} manifest filename/content mismatch")
        summaries[experiment] = validate_manifest(manifest)
        digests[experiment] = str(manifest["manifest_sha256"])
    frozen_set = json.dumps(digests, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "phase": "validate",
        "case_counts": {key: cast(int, value["case_count"]) for key, value in summaries.items()},
        "manifest_sha256": digests,
        "manifest_set_sha256": hashlib.sha256(frozen_set).hexdigest(),
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _current_l0_counts(manifest: dict[str, Any]) -> dict[str, object]:
    decisions: Counter[str] = Counter()
    authority = {"low": 1, "medium": 2, "high": 3}
    resolved = 0
    for item in manifest["cases"]:
        docket = item.get("input", {})
        case = docket.get("case", {})
        claims = {claim.get("id"): claim for claim in docket.get("claims", [])}
        left = claims.get(case.get("left_claim_id"), {})
        right = claims.get(case.get("right_claim_id"), {})
        left_score = authority.get(left.get("source_authority"))
        right_score = authority.get(right.get("source_authority"))
        if case.get("group_key") is None and left_score and right_score and left_score != right_score:
            decisions["keep_left" if left_score > right_score else "keep_right"] += 1
            resolved += 1
        else:
            decisions["manual_required"] += 1
    return {
        "decision_counts": dict(sorted(decisions.items())),
        "deferred_missing_predecision_state": len(manifest["cases"]) - resolved,
        "policy": "v0.29.3-authority-observable-subset",
        "resolved_by_authority": resolved,
    }


def run_baseline(
    manifest_dir: str | Path,
    database_path: str | Path,
    config_path: str | Path,
    recall_path: str | Path,
    output_dir: str | Path,
    *,
    arm: str = "A",
) -> dict[str, object]:
    """Run the read-only current-behavior arm and write deterministic evidence."""

    if arm != "A":
        raise ValueError("batch 0 baseline only permits the A arm")
    manifests = validate_manifest_directory(manifest_dir)
    database = Path(database_path)
    with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        dedup_counts = dict(
            connection.execute(
                "SELECT COALESCE(decision, '<null>'), COUNT(*) FROM dedup_pairs GROUP BY decision"
            ).fetchall()
        )
        dedup_row = connection.execute(
            "SELECT COUNT(*), COUNT(reviewed_at), COUNT(applied_at) FROM dedup_pairs"
        ).fetchone()
        open_count = connection.execute("SELECT COUNT(*) FROM conflict_cases WHERE resolved_at IS NULL").fetchone()[0]
        stable_count = connection.execute("""SELECT COUNT(*) FROM conflict_cases c
               LEFT JOIN conflict_review_state r ON r.case_id=c.id
               WHERE c.status='manual_required' AND c.resolved_at IS NULL
                 AND (r.case_id IS NULL OR r.dirty_at IS NULL)""").fetchone()[0]
    config = tomllib.loads(Path(config_path).read_text(encoding="utf-8"))
    recall = json.loads(Path(recall_path).read_text(encoding="utf-8"))
    payload: dict[str, object] = {
        "arm": "A",
        "conflicts": {"open": open_count, "stable_manual_required": stable_count},
        "dedup": {
            "applied": dedup_row[2],
            "audit_only": bool(config.get("dedup", {}).get("audit_only", True)),
            "decision_counts": dict(sorted(dedup_counts.items())),
            "reviewed": dedup_row[1],
            "total": dedup_row[0],
        },
        "e1_l0": _current_l0_counts(load_manifest(Path(manifest_dir) / "e1.json")),
        "inputs": {
            "config_sha256": _file_sha256(Path(config_path)),
            "database_sha256": _file_sha256(database),
            "manifest_set_sha256": manifests["manifest_set_sha256"],
            "recall_sha256": _file_sha256(Path(recall_path)),
        },
        "recall": {
            "dataset_sha256": recall.get("dataset_sha256", recall.get("dataset_hash")),
            "metrics": recall["metrics"],
        },
        "schema_version": "v030-baseline-v1",
    }
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    baseline = root / "baseline.json"
    summary = root / "summary.md"
    baseline.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary.write_text(
        "# v0.30 A-arm baseline\n\n"
        "This replay covers only the v0.29.3 authority branch observable from frozen dockets; deferred cases are not reconstructed.\n\n"
        f"- E1 current-L0 decisions: `{json.dumps(payload['e1_l0'], sort_keys=True)}`\n"
        f"- Dedup: `{json.dumps(payload['dedup'], sort_keys=True)}`\n"
        f"- Conflicts: `{json.dumps(payload['conflicts'], sort_keys=True)}`\n"
        f"- Recall: `{json.dumps(payload['recall'], sort_keys=True)}`\n",
        encoding="utf-8",
    )
    sums = [f"{_file_sha256(path)}  {path.name}" for path in (baseline, summary)]
    (root / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="ascii")
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("validate", "baseline"))
    parser.add_argument("--manifest-dir", type=Path, required=True)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--recall-baseline", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--arm", default="A")
    args = parser.parse_args(argv)
    if args.phase == "validate":
        result = validate_manifest_directory(args.manifest_dir)
    else:
        required = (args.database, args.config, args.recall_baseline, args.output_dir)
        if any(path is None for path in required):
            parser.error("baseline requires --database, --config, --recall-baseline and --output-dir")
        result = run_baseline(
            args.manifest_dir,
            args.database,
            args.config,
            args.recall_baseline,
            args.output_dir,
            arm=args.arm,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
