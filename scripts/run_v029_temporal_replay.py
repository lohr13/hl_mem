#!/usr/bin/env python
"""Run the fixed v0.29.0 temporal-link gate on a read-only production snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hl_mem.domain.claims.temporal_links import TEMPORAL_LINK_RULE_VERSION, evaluate_temporal_link

PRICE_CORRECTION_EVENT_IDS = (
    "657745666e1341179e7095f8c99fc4ec",
    "008e413be3094e74af7cafb6a1c56b9b",
    "137e0d5b08d543499b4df80a2bbd7769",
    "d5ab712574c14dbeb94cea486313927e",
    "92c57e0e25f9436b9172cb178057ec99",
    "8d0f5022c3524df7b216020a274cb64f",
    "2b32f0e6854b4f2cb6bed17d013561c0",
    "c7909a1d780c461cb0a5be8bc4a499cf",
    "9067f613937e481bb2351664103a88f8",
    "231173ace30f499895ddbc08ed9198bc",
    "f34d18dae3e248e69fa12a6acb9b8b2f",
    "819d07506e6e44ffa890fd5d76d6c4d9",
    "50a1fb6740024d6f91e91434b5f14b5f",
    "9d2dcaf1ea5844c7b4590ed1fb780fe2",
)
TAILSCALE_SNAPSHOT_IDS = (
    "d26948807963460590703ee1b4b7c0a3",
    "c844a1a27e2945c5800e499febce41e2",
    "a7f5ea83e2554825adfeec029dbd63b4",
)
_AUTO_OUTCOMES = frozenset({"entails", "state_change"})
_MISSING_PRICE_CLAIMS: dict[str, dict[str, Any]] = {
    "6b25faed6e194ec687f369cc6f0b2666": {
        "id": "6b25faed6e194ec687f369cc6f0b2666",
        "namespace_key": "default",
        "subject_entity_id": "阿里百炼",
        "predicate": "事实",
        "canonical_attribute": "fact.other",
        "canonical_slot": None,
        "qualifiers": {},
        "value": "输入价格为 ¥1/百万 tokens",
        "valid_from": "2026-08-09T13:39:50+00:00",
        "recorded_from": "2026-08-09T13:40:46+00:00",
        "source_authority": "low",
        "assertion_kind": "unknown",
        "status": "active",
    }
}


class ReplayGateError(RuntimeError):
    """Raised after the report is written when a hard release gate fails."""


def enforce_gate(report: dict[str, Any]) -> None:
    """Enforce the preregistered precision and coexistence vetoes."""

    gates = report["gates"]
    if not gates["passed"]:
        raise ReplayGateError(
            "v0.29.0 temporal replay gate failed: "
            f"precision={gates['price_precision_actual']}, "
            f"coverage={gates['price_coverage_actual']}, "
            f"false_coexist_links={gates['false_coexist_links_actual']}, "
            f"tailscale_passed={gates['tailscale_sequence_passed']}"
        )


def run_replay(source_path: Path, output_dir: Path) -> tuple[dict[str, Any], Path, Path]:
    """Back up a query-only source, evaluate the immutable copy, and return report paths."""

    source_path = source_path.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    copy_path = output_dir / f"v0290-temporal-replay-{stamp}.db"
    report_path = output_dir / f"v0290-temporal-replay-{stamp}.json"

    source_sha256 = _sha256(source_path)
    _backup_read_only(source_path, copy_path)
    copy_sha256 = _sha256(copy_path)
    connection = _open_read_only(copy_path, immutable=True)
    try:
        report = _evaluate_copy(connection)
    finally:
        connection.close()
    report["metadata"] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_path": str(source_path),
        "source_open_mode": "mode=ro + PRAGMA query_only=ON",
        "source_sha256": source_sha256,
        "replay_copy_path": str(copy_path),
        "replay_copy_open_mode": "mode=ro + immutable=1 + PRAGMA query_only=ON",
        "replay_copy_sha256": copy_sha256,
        "schema_migration_count": _schema_migration_count(connection=None, copy_path=copy_path),
        "code_commit": _git_commit(),
        "rule_version": TEMPORAL_LINK_RULE_VERSION,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report, copy_path, report_path


def _evaluate_copy(connection: sqlite3.Connection) -> dict[str, Any]:
    price_cases = _price_cases(connection)
    predicted_price = sum(case["actual_outcome"] == "state_change" for case in price_cases)
    correct_price = sum(case["actual_outcome"] == case["expected_outcome"] == "state_change" for case in price_cases)
    price_precision = correct_price / predicted_price if predicted_price else 0.0
    price_coverage = correct_price / len(PRICE_CORRECTION_EVENT_IDS)

    tailscale_cases = _tailscale_cases(connection)
    tailscale_passed = all(case["actual_outcome"] == case["expected_outcome"] for case in tailscale_cases)

    coexistence = _coexistence_cases(connection)
    false_coexist_links = sum(
        case["actual_outcome"] in _AUTO_OUTCOMES for cohort in coexistence.values() for case in cohort["comparisons"]
    )
    gates = {
        "price_precision_required": 1.0,
        "price_precision_actual": price_precision,
        "price_coverage_required": 1.0,
        "price_coverage_actual": price_coverage,
        "false_coexist_links_required": 0,
        "false_coexist_links_actual": false_coexist_links,
        "tailscale_sequence_passed": tailscale_passed,
    }
    gates["passed"] = (
        price_precision == gates["price_precision_required"]
        and price_coverage == gates["price_coverage_required"]
        and false_coexist_links == gates["false_coexist_links_required"]
        and tailscale_passed
    )
    return {
        "price_corrections": {
            "expected_cases": len(PRICE_CORRECTION_EVENT_IDS),
            "predicted_links": predicted_price,
            "correct_links": correct_price,
            "precision": price_precision,
            "coverage": price_coverage,
            "cases": price_cases,
        },
        "tailscale_three_snapshots": {
            "snapshot_ids": list(TAILSCALE_SNAPSHOT_IDS),
            "passed": tailscale_passed,
            "cases": tailscale_cases,
        },
        "coexistence_veto": {
            "false_links": false_coexist_links,
            "cohorts": coexistence,
        },
        "gates": gates,
    }


def _price_cases(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for event_id in PRICE_CORRECTION_EVENT_IDS:
        row = connection.execute(
            "SELECT content_json,recorded_at FROM events WHERE id=? AND event_type='correction'",
            (event_id,),
        ).fetchone()
        if row is None:
            raise ReplayGateError(f"fixed correction event missing: {event_id}")
        content = json.loads(row["content_json"])
        old, old_fallback = _load_claim(connection, str(content["memory_id"]))
        new, new_fallback = _load_claim(connection, str(content["new_claim_id"]))
        if old is None or new is None:
            raise ReplayGateError(f"correction endpoints missing: {event_id}")
        new["assertion_kind"] = "observation"
        decision = evaluate_temporal_link(old, new)
        cases.append(
            {
                "event_id": event_id,
                "old_claim_id": old["id"],
                "new_claim_id": new["id"],
                "used_fixed_old_fallback": old_fallback,
                "used_fixed_new_fallback": new_fallback,
                "expected_outcome": "state_change",
                "actual_outcome": decision.outcome,
                "rule_id": decision.rule_id,
                "rationale": decision.rationale,
            }
        )
    return cases


def _tailscale_cases(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for claim_id in TAILSCALE_SNAPSHOT_IDS:
        claim, used_fallback = _load_claim(connection, claim_id)
        if claim is None or used_fallback:
            raise ReplayGateError(f"fixed Tailscale snapshot missing: {claim_id}")
        snapshots.append(claim)
    comparisons = ((0, 1, "entails"), (1, 2, "state_change"))
    cases: list[dict[str, Any]] = []
    for old_index, new_index, expected in comparisons:
        new = dict(snapshots[new_index])
        new["assertion_kind"] = "observation"
        decision = evaluate_temporal_link(snapshots[old_index], new)
        cases.append(
            {
                "old_claim_id": snapshots[old_index]["id"],
                "new_claim_id": new["id"],
                "expected_outcome": expected,
                "actual_outcome": decision.outcome,
                "rule_id": decision.rule_id,
                "rationale": decision.rationale,
            }
        )
    return cases


def _coexistence_cases(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    specs = {
        "config_path_120_active": ("config.path", "hl_mem", 120),
        "config_network_xiaoman_active": ("config.network", "小满", 4),
    }
    cohorts: dict[str, dict[str, Any]] = {}
    for name, (attribute, subject, required_count) in specs.items():
        rows = connection.execute(
            "SELECT * FROM claims WHERE namespace_key='default' AND subject_entity_id=? "
            "AND canonical_attribute=? AND status='active' ORDER BY recorded_from,id LIMIT ?",
            (subject, attribute, required_count),
        ).fetchall()
        if len(rows) != required_count:
            raise ReplayGateError(f"fixed coexistence cohort {name} expected {required_count}, found {len(rows)}")
        claims = [_decode_claim(dict(row)) for row in rows]
        comparisons: list[dict[str, Any]] = []
        for old, raw_new in zip(claims, claims[1:]):
            new = dict(raw_new)
            new["assertion_kind"] = "observation"
            decision = evaluate_temporal_link(old, new)
            comparisons.append(
                {
                    "old_claim_id": old["id"],
                    "new_claim_id": new["id"],
                    "actual_outcome": decision.outcome,
                    "rule_id": decision.rule_id,
                    "rationale": decision.rationale,
                }
            )
        cohorts[name] = {
            "active_claims": len(claims),
            "comparisons": comparisons,
            "false_links": sum(case["actual_outcome"] in _AUTO_OUTCOMES for case in comparisons),
        }
    return cohorts


def _load_claim(connection: sqlite3.Connection, claim_id: str) -> tuple[dict[str, Any] | None, bool]:
    row = connection.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
    if row is None:
        fallback = _MISSING_PRICE_CLAIMS.get(claim_id)
        return (dict(fallback), True) if fallback is not None else (None, False)
    return _decode_claim(dict(row)), False


def _decode_claim(claim: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(claim.pop("value_json"))
    if isinstance(value, dict) and value.get("_type") == "superseded_value":
        value = value.get("old_value")
    claim["value"] = value
    claim["qualifiers"] = json.loads(claim.pop("qualifiers_json") or "{}")
    claim.setdefault("assertion_kind", "unknown")
    return claim


def _open_read_only(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    suffix = "?mode=ro&immutable=1" if immutable else "?mode=ro"
    connection = sqlite3.connect(path.resolve().as_uri() + suffix, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _backup_read_only(source_path: Path, copy_path: Path) -> None:
    source = _open_read_only(source_path)
    destination = sqlite3.connect(copy_path)
    try:
        source.backup(destination)
    except Exception:
        destination.close()
        source.close()
        copy_path.unlink(missing_ok=True)
        raise
    destination.close()
    source.close()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _schema_migration_count(*, connection: sqlite3.Connection | None, copy_path: Path) -> int:
    owned = connection is None
    current = connection or _open_read_only(copy_path, immutable=True)
    try:
        return int(current.execute("SELECT count(*) FROM schema_migrations").fetchone()[0])
    finally:
        if owned:
            current.close()


def _git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Production SQLite database opened read-only")
    parser.add_argument("--output-dir", type=Path, default=Path("var/eval"))
    args = parser.parse_args()
    report, copy_path, report_path = run_replay(args.source, args.output_dir)
    print(json.dumps({"copy": str(copy_path), "report": str(report_path), "gates": report["gates"]}, indent=2))
    enforce_gate(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
