"""按统一 slot/attribute 规则回填短 TTL Claim。"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from hl_mem.config_loader import load_settings
from hl_mem.domain.claims.retention import (
    TTLPolicy,
    compute_expiration,
    is_short_ttl_classification,
)
from hl_mem.storage.database import Database
from hl_mem.workers.read_only import open_read_only_connection


def _short_ttl_rows(connection: Any, policy: TTLPolicy) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT id,status,scope,importance,volatility,canonical_attribute,canonical_slot,"
        "valid_to,observed_at,recorded_from,expires_at FROM claims ORDER BY id"
    ).fetchall()
    return [
        dict(row)
        for row in rows
        if is_short_ttl_classification(row["canonical_slot"], row["canonical_attribute"], policy)
    ]


def backfill_short_ttl(
    connection: Any,
    policy: TTLPolicy,
    *,
    dry_run: bool = True,
) -> dict[str, int | bool]:
    """预览或把所有有效短 TTL 分类统一为 temporal + 绝对短 TTL。"""
    if not dry_run:
        connection.execute("BEGIN IMMEDIATE")
    try:
        rows = _short_ttl_rows(connection, policy)
        updated = 0
        scope_changed = 0
        expires_at_changed = 0
        applied = 0
        cas_skipped = 0
        for claim in rows:
            expires_at, _reason = compute_expiration(
                scope="temporal",
                importance=float(claim.get("importance") or 0.5),
                volatility=str(claim.get("volatility") or "stable"),
                canonical_slot=claim.get("canonical_slot"),
                canonical_attribute=claim.get("canonical_attribute"),
                valid_to=claim.get("valid_to"),
                observed_at=str(claim.get("observed_at") or ""),
                recorded_from=str(claim["recorded_from"]),
                policy=policy,
            )
            changes_scope = claim.get("scope") != "temporal"
            changes_expiration = claim.get("expires_at") != expires_at
            if not changes_scope and not changes_expiration:
                continue
            updated += 1
            scope_changed += int(changes_scope)
            expires_at_changed += int(changes_expiration)
            if dry_run:
                continue
            cursor = connection.execute(
                "UPDATE claims SET scope='temporal',expires_at=? WHERE id=? AND status=? AND scope=? "
                "AND importance IS ? AND volatility=? AND canonical_attribute IS ? AND canonical_slot IS ? "
                "AND valid_to IS ? AND observed_at IS ? AND recorded_from=? AND expires_at IS ?",
                (
                    expires_at,
                    claim["id"],
                    claim["status"],
                    claim["scope"],
                    claim["importance"],
                    claim["volatility"],
                    claim["canonical_attribute"],
                    claim["canonical_slot"],
                    claim["valid_to"],
                    claim["observed_at"],
                    claim["recorded_from"],
                    claim["expires_at"],
                ),
            )
            if cursor.rowcount == 1:
                applied += 1
            else:
                cas_skipped += 1
        if not dry_run:
            connection.commit()
        return {
            "matched": len(rows),
            "updated": updated,
            "scope_changed": scope_changed,
            "expires_at_changed": expires_at_changed,
            "applied": applied,
            "cas_skipped": cas_skipped,
            "dry_run": dry_run,
        }
    except Exception:
        if not dry_run and connection.in_transaction:
            connection.rollback()
        raise


def main() -> None:
    """运行短 TTL 回填；省略 --apply 时仅输出 dry-run 统计。"""
    parser = argparse.ArgumentParser(prog="python -m hl_mem.workers.backfill_short_ttl")
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
            result = backfill_short_ttl(connection, settings.retention_policy())
        finally:
            connection.close()
    else:
        database = Database(settings=settings)
        try:
            result = backfill_short_ttl(
                database.open(),
                settings.retention_policy(),
                dry_run=False,
            )
        finally:
            database.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
