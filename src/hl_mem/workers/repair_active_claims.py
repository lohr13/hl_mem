"""Audit and safely converge duplicate active claims.

``audit`` and ``repair --dry-run`` open SQLite in query-only mode. ``repair
--apply`` is the sole write mode and performs the complete plan in one
``BEGIN IMMEDIATE`` transaction.
"""

from __future__ import annotations

import argparse
import json
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Any, Sequence

from hl_mem.config_loader import load_settings
from hl_mem.domain.claims.attributes import MUTUALLY_EXCLUSIVE_SLOTS
from hl_mem.domain.claims.conflicts import compute_claim_pair_key
from hl_mem.lifecycle import assert_transition
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.workers.read_only import open_read_only_connection

ACTIVE_EXACT_DUPLICATES_SQL = (
    "SELECT namespace_key,fact_hash,count(*) AS active_count FROM claims "
    "WHERE status='active' AND fact_hash IS NOT NULL "
    "GROUP BY namespace_key,fact_hash HAVING count(*)>1 "
    "ORDER BY namespace_key,fact_hash"
)
TERMINAL_CONFLICT_CASE_STATUSES = frozenset({"resolved", "rejected"})


def _exclusive_conflicts_sql() -> tuple[str, tuple[str, ...]]:
    slots = tuple(sorted(MUTUALLY_EXCLUSIVE_SLOTS))
    placeholders = ",".join("?" for _ in slots)
    return (
        "SELECT namespace_key,conflict_key,count(*) AS active_count FROM claims "
        "WHERE status='active' AND conflict_key IS NOT NULL "
        f"AND canonical_slot IN ({placeholders}) "
        "GROUP BY namespace_key,conflict_key HAVING count(*)>1 "
        "ORDER BY namespace_key,conflict_key",
        slots,
    )


def _claim_ids_for_exact(connection: Any, namespace: str, fact_hash: str) -> list[str]:
    return [
        str(row["id"])
        for row in connection.execute(
            "SELECT id FROM claims WHERE namespace_key=? AND fact_hash=? AND status='active' "
            "ORDER BY recorded_from,id",
            (namespace, fact_hash),
        ).fetchall()
    ]


def _claims_for_conflict(connection: Any, namespace: str, conflict_key: str) -> list[dict[str, str]]:
    return [
        {
            "id": str(row["id"]),
            "canonical_slot": str(row["canonical_slot"]),
            "fact_hash": str(row["fact_hash"] or ""),
        }
        for row in connection.execute(
            "SELECT id,canonical_slot,fact_hash FROM claims "
            "WHERE namespace_key=? AND conflict_key=? AND status='active' ORDER BY recorded_from,id",
            (namespace, conflict_key),
        ).fetchall()
    ]


def audit_active_claims(connection: Any) -> dict[str, Any]:
    """Run the productized read-only invariant audit queries."""
    exact_groups = []
    for row in connection.execute(ACTIVE_EXACT_DUPLICATES_SQL).fetchall():
        namespace = str(row["namespace_key"])
        fact_hash = str(row["fact_hash"])
        exact_groups.append(
            {
                "namespace_key": namespace,
                "fact_hash": fact_hash,
                "active_count": int(row["active_count"]),
                "claim_ids": _claim_ids_for_exact(connection, namespace, fact_hash),
            }
        )

    sql, parameters = _exclusive_conflicts_sql()
    conflict_groups = []
    by_slot: dict[str, dict[str, int]] = {}
    for row in connection.execute(sql, parameters).fetchall():
        namespace = str(row["namespace_key"])
        conflict_key = str(row["conflict_key"])
        claims = _claims_for_conflict(connection, namespace, conflict_key)
        slots = sorted({claim["canonical_slot"] for claim in claims})
        slot = slots[0] if len(slots) == 1 else "mixed"
        conflict_groups.append(
            {
                "namespace_key": namespace,
                "conflict_key": conflict_key,
                "canonical_slot": slot,
                "active_count": int(row["active_count"]),
                "claim_ids": [claim["id"] for claim in claims],
            }
        )
        counts = by_slot.setdefault(slot, {"groups": 0, "claims": 0})
        counts["groups"] += 1
        counts["claims"] += len(claims)

    return {
        "healthy": not exact_groups and not conflict_groups,
        "exact_duplicates": {
            "group_count": len(exact_groups),
            "claim_count": sum(group["active_count"] for group in exact_groups),
            "groups": exact_groups,
        },
        "exclusive_conflicts": {
            "group_count": len(conflict_groups),
            "claim_count": sum(group["active_count"] for group in conflict_groups),
            "by_slot": dict(sorted(by_slot.items())),
            "groups": conflict_groups,
        },
    }


def _select_exact_winner(connection: Any, claim_ids: Sequence[str]) -> str:
    placeholders = ",".join("?" for _ in claim_ids)
    row = connection.execute(
        "SELECT c.id,("
        "SELECT count(*) FROM evidence_links e WHERE "
        "(e.derived_type='claim' AND e.derived_id=c.id) OR "
        "(e.evidence_type='claim' AND e.evidence_id=c.id)"
        ") AS reference_count "
        f"FROM claims c WHERE c.id IN ({placeholders}) "
        "ORDER BY reference_count DESC,c.recorded_from,c.id LIMIT 1",
        tuple(claim_ids),
    ).fetchone()
    if row is None:
        raise RuntimeError("exact-duplicate repair group disappeared")
    return str(row["id"])


def _existing_case(connection: Any, pair_key: str) -> dict[str, str] | None:
    row = connection.execute(
        "SELECT id,status FROM conflict_cases WHERE pair_key=?",
        (pair_key,),
    ).fetchone()
    return {"id": str(row["id"]), "status": str(row["status"])} if row is not None else None


def _build_repair_plan(connection: Any) -> dict[str, Any]:
    audit = audit_active_claims(connection)
    exact_actions: list[dict[str, Any]] = []
    exact_losers: set[str] = set()
    for group in audit["exact_duplicates"]["groups"]:
        claim_ids = list(group["claim_ids"])
        winner = _select_exact_winner(connection, claim_ids)
        losers = [claim_id for claim_id in claim_ids if claim_id != winner]
        exact_losers.update(losers)
        exact_actions.append({"winner_id": winner, "loser_ids": losers})

    conflict_actions: list[dict[str, Any]] = []
    for group in audit["exclusive_conflicts"]["groups"]:
        claim_ids = [claim_id for claim_id in group["claim_ids"] if claim_id not in exact_losers]
        if len(claim_ids) < 2:
            continue
        pairs = []
        for left, right in combinations(claim_ids, 2):
            pair_key = compute_claim_pair_key(left, right)
            pairs.append(
                {
                    "left_claim_id": left,
                    "right_claim_id": right,
                    "pair_key": pair_key,
                    "existing_case": _existing_case(connection, pair_key),
                }
            )
        conflict_actions.append(
            {
                "namespace_key": group["namespace_key"],
                "conflict_key": group["conflict_key"],
                "claim_ids": claim_ids,
                "pairs": pairs,
            }
        )

    conflict_claim_ids = {claim_id for action in conflict_actions for claim_id in action["claim_ids"]}
    all_pairs = [pair for action in conflict_actions for pair in action["pairs"]]
    pairs_to_create = [pair for pair in all_pairs if pair["existing_case"] is None]
    pairs_to_reopen = [
        pair
        for pair in all_pairs
        if pair["existing_case"] is not None
        and pair["existing_case"]["status"] != "manual_required"
        and pair["existing_case"]["status"] not in TERMINAL_CONFLICT_CASE_STATUSES
    ]
    terminal_pairs = [
        pair
        for pair in all_pairs
        if pair["existing_case"] is not None and pair["existing_case"]["status"] in TERMINAL_CONFLICT_CASE_STATUSES
    ]
    summary = {
        "exact_duplicate_groups": len(exact_actions),
        "exact_duplicate_claims_to_supersede": len(exact_losers),
        "conflict_groups_to_dispute": len(conflict_actions),
        "claims_to_dispute": len(conflict_claim_ids),
        "manual_conflict_cases_to_create": len(pairs_to_create),
        "manual_conflict_cases_to_reopen": len(pairs_to_reopen),
        "terminal_conflict_cases_to_preserve": len(terminal_pairs),
    }
    return {
        "audit": audit,
        "exact_actions": exact_actions,
        "conflict_actions": conflict_actions,
        "summary": summary,
    }


def _copy_event_evidence(connection: Any, loser_id: str, winner_id: str) -> None:
    connection.execute(
        "INSERT INTO evidence_links(id,derived_type,derived_id,evidence_type,evidence_id,relation,weight) "
        "SELECT lower(hex(randomblob(16))),'claim',?,source.evidence_type,source.evidence_id,"
        "source.relation,source.weight FROM evidence_links source "
        "WHERE source.derived_type='claim' AND source.derived_id=? AND source.evidence_type='event' "
        "AND NOT EXISTS (SELECT 1 FROM evidence_links existing WHERE existing.derived_type='claim' "
        "AND existing.derived_id=? AND existing.evidence_type=source.evidence_type "
        "AND existing.evidence_id=source.evidence_id AND existing.relation=source.relation)",
        (winner_id, loser_id, winner_id),
    )


def repair_active_claims(
    connection: Any,
    *,
    apply: bool,
    repaired_at: str | None = None,
) -> dict[str, Any]:
    """Preview or transactionally apply active-claim convergence."""
    if not apply:
        built = _build_repair_plan(connection)
        return {
            "dry_run": True,
            "before": built["audit"],
            "plan": built["summary"],
            "applied": {
                "exact_duplicate_claims_superseded": 0,
                "claims_disputed": 0,
                "manual_conflict_cases_created": 0,
                "manual_conflict_cases_reopened": 0,
                "terminal_conflict_cases_preserved": 0,
            },
            "after": built["audit"],
        }

    timestamp = repaired_at or datetime.now(timezone.utc).isoformat()
    connection.execute("BEGIN IMMEDIATE")
    try:
        built = _build_repair_plan(connection)
        repository = ClaimRepository(connection)
        exact_superseded = 0
        for action in built["exact_actions"]:
            winner = repository.get_claim(action["winner_id"])
            if winner is None:
                raise RuntimeError(f"exact-duplicate winner disappeared: {action['winner_id']}")
            for loser_id in action["loser_ids"]:
                _copy_event_evidence(connection, loser_id, action["winner_id"])
                result = repository.supersede_with_inline(
                    loser_id,
                    action["winner_id"],
                    winner.get("value"),
                    timestamp,
                    timestamp,
                    commit=False,
                )
                if not result.applied:
                    raise RuntimeError(f"exact-duplicate loser changed during repair: {loser_id}")
                exact_superseded += 1

        disputed = 0
        created_cases = 0
        reopened_cases = 0
        preserved_terminal_cases = 0
        for action in built["conflict_actions"]:
            for claim_id in action["claim_ids"]:
                assert_transition("active", "disputed")
                cursor = connection.execute(
                    "UPDATE claims SET status='disputed' WHERE id=? AND status='active'",
                    (claim_id,),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(f"active conflict claim changed during repair: {claim_id}")
                disputed += 1
            for pair in action["pairs"]:
                existing_case = pair["existing_case"]
                if existing_case is None:
                    cursor = connection.execute(
                        "INSERT INTO conflict_cases("
                        "id,pair_key,left_claim_id,right_claim_id,status,decision,rationale,confidence,created_at"
                        ") VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            uuid.uuid4().hex,
                            pair["pair_key"],
                            pair["left_claim_id"],
                            pair["right_claim_id"],
                            "manual_required",
                            "uncertain",
                            "active_claim_invariant_repair",
                            None,
                            timestamp,
                        ),
                    )
                    created_cases += cursor.rowcount
                elif existing_case["status"] in TERMINAL_CONFLICT_CASE_STATUSES:
                    preserved_terminal_cases += 1
                elif existing_case["status"] != "manual_required":
                    cursor = connection.execute(
                        "UPDATE conflict_cases SET status='manual_required',decision='uncertain',"
                        "rationale='active_claim_invariant_repair',confidence=NULL,resolved_at=NULL "
                        "WHERE id=? AND status=?",
                        (existing_case["id"], existing_case["status"]),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError(f"conflict case changed during repair: {existing_case['id']}")
                    reopened_cases += 1
                case_status = connection.execute(
                    "SELECT status FROM conflict_cases WHERE pair_key=?",
                    (pair["pair_key"],),
                ).fetchone()
                expected_status = (
                    existing_case["status"]
                    if existing_case is not None and existing_case["status"] in TERMINAL_CONFLICT_CASE_STATUSES
                    else "manual_required"
                )
                if case_status is None or case_status["status"] != expected_status:
                    raise RuntimeError(f"conflict pair did not reach its planned state: {pair['pair_key']}")
        after = audit_active_claims(connection)
        if not after["healthy"]:
            raise RuntimeError("active-claim repair did not converge all audited invariants")
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    return {
        "dry_run": False,
        "before": built["audit"],
        "plan": built["summary"],
        "applied": {
            "exact_duplicate_claims_superseded": exact_superseded,
            "claims_disputed": disputed,
            "manual_conflict_cases_created": created_cases,
            "manual_conflict_cases_reopened": reopened_cases,
            "terminal_conflict_cases_preserved": preserved_terminal_cases,
        },
        "after": after,
    }


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m hl_mem.workers.repair_active_claims")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--db")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("audit", help="run read-only active-claim invariant audit")
    repair = commands.add_parser("repair", help="preview or apply active-claim convergence")
    modes = repair.add_mutually_exclusive_group(required=True)
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--apply", action="store_true")
    return parser.parse_args(argv)


def main() -> None:
    args = _parse_args()
    settings = load_settings(args.config, args.env_file)
    if args.db is not None:
        settings = replace(settings, database_path=args.db)
    if args.command == "audit" or getattr(args, "dry_run", False):
        connection = open_read_only_connection(
            settings.database_path,
            busy_timeout_seconds=settings.database_busy_timeout_seconds,
        )
        try:
            result = (
                audit_active_claims(connection)
                if args.command == "audit"
                else repair_active_claims(connection, apply=False)
            )
        finally:
            connection.close()
    else:
        database = Database(settings=settings)
        try:
            result = repair_active_claims(database.open(), apply=True)
        finally:
            database.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
