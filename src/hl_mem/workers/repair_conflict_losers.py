"""修复已 resolved 冲突中仍停留在 disputed 的败者 Claim。"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from hl_mem.config_loader import load_settings
from hl_mem.lifecycle import assert_transition
from hl_mem.storage.database import Database
from hl_mem.workers.read_only import open_read_only_connection


def _repair_candidates(connection: Any) -> list[dict[str, str]]:
    rows = connection.execute(
        "SELECT id,left_claim_id,right_claim_id,decision,resolved_at FROM conflict_cases "
        "WHERE status='resolved' AND decision IN ('keep_left','keep_right') AND resolved_at IS NOT NULL "
        "ORDER BY id"
    ).fetchall()
    candidates: list[dict[str, str]] = []
    for row in rows:
        winner_id = row["left_claim_id"] if row["decision"] == "keep_left" else row["right_claim_id"]
        loser_id = row["right_claim_id"] if row["decision"] == "keep_left" else row["left_claim_id"]
        statuses = {
            claim_row["id"]: claim_row["status"]
            for claim_row in connection.execute(
                "SELECT id,status FROM claims WHERE id IN (?,?)",
                (winner_id, loser_id),
            ).fetchall()
        }
        if winner_id not in statuses or statuses.get(loser_id) != "disputed":
            continue
        candidates.append(
            {
                "case_id": str(row["id"]),
                "winner_id": str(winner_id),
                "loser_id": str(loser_id),
                "resolved_at": str(row["resolved_at"]),
            }
        )
    return candidates


def repair_conflict_losers(connection: Any, *, dry_run: bool = True) -> dict[str, int | bool]:
    """预览或修复 resolved case 的 disputed 败者，默认不写数据库。"""
    candidates = _repair_candidates(connection)
    if dry_run:
        return {
            "matched": len(candidates),
            "repaired": 0,
            "cas_skipped": 0,
            "dry_run": True,
        }

    repaired = 0
    cas_skipped = 0
    connection.execute("BEGIN IMMEDIATE")
    try:
        for candidate in _repair_candidates(connection):
            assert_transition("disputed", "superseded")
            cursor = connection.execute(
                "UPDATE claims SET status='superseded',valid_to=?,recorded_to=?,superseded_by_id=? "
                "WHERE id=? AND status='disputed'",
                (
                    candidate["resolved_at"],
                    candidate["resolved_at"],
                    candidate["winner_id"],
                    candidate["loser_id"],
                ),
            )
            if cursor.rowcount == 1:
                repaired += 1
            else:
                cas_skipped += 1
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    return {
        "matched": len(candidates),
        "repaired": repaired,
        "cas_skipped": cas_skipped,
        "dry_run": False,
    }


def main() -> None:
    """运行冲突败者修复；省略 --apply 时仅输出 dry-run 统计。"""
    parser = argparse.ArgumentParser(prog="python -m hl_mem.workers.repair_conflict_losers")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--db")
    parser.add_argument("--apply", action="store_true", help="实际写入；省略时为 dry-run")
    args = parser.parse_args()
    settings = load_settings(args.config, args.env_file)
    if args.db is not None:
        settings = replace(settings, database_path=args.db)
    if not args.apply:
        connection = open_read_only_connection(
            settings.database_path,
            busy_timeout_seconds=settings.database_busy_timeout_seconds,
        )
        try:
            result = repair_conflict_losers(connection)
        finally:
            connection.close()
    else:
        database = Database(settings=settings)
        try:
            result = repair_conflict_losers(database.open(), dry_run=False)
        finally:
            database.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
