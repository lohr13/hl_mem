"""为存量 temporal claim 分批回填三因子 TTL。"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from hl_mem.domain.claims.retention import TTLPolicy, compute_expiration
from hl_mem.settings import Settings
from hl_mem.storage.database import Database

PROTECTED_ATTRIBUTES = frozenset({"memory.explicit", "identity.name"})


def _as_utc(value: datetime) -> datetime:
    """把有无时区的时间统一为 UTC，便于稳定比较。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def backfill_expires_at(
    connection: Any,
    policy: TTLPolicy,
    *,
    dry_run: bool = True,
    batch_size: int = 100,
    grace_period: timedelta = timedelta(0),
    now: datetime | None = None,
) -> dict[str, int]:
    """分批重算 active temporal claim 的 expires_at，并按宽限期执行过期。"""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if grace_period < timedelta(0):
        raise ValueError("grace_period must be non-negative")
    current_time = _as_utc(now or datetime.now(timezone.utc))
    expiration_cutoff = current_time - grace_period
    scanned = updated = expired = skipped_protected = 0
    last_id = ""

    while True:
        rows = connection.execute(
            "SELECT id,scope,importance,volatility,canonical_attribute,canonical_slot,"
            "valid_to,observed_at,recorded_from,expires_at "
            "FROM claims WHERE status='active' AND scope='temporal' AND id>? "
            "ORDER BY id LIMIT ?",
            (last_id, batch_size),
        ).fetchall()
        if not rows:
            break
        for raw_row in rows:
            claim = dict(raw_row)
            last_id = str(claim["id"])
            scanned += 1
            if claim.get("canonical_attribute") in PROTECTED_ATTRIBUTES:
                skipped_protected += 1
                continue
            expires_at, _reason = compute_expiration(
                scope=str(claim["scope"]),
                importance=float(claim.get("importance") or 0.5),
                volatility=str(claim.get("volatility") or "stable"),
                canonical_slot=claim.get("canonical_slot"),
                valid_to=claim.get("valid_to"),
                observed_at=str(claim.get("observed_at") or ""),
                recorded_from=str(claim["recorded_from"]),
                policy=policy,
            )
            updated += int(expires_at != claim.get("expires_at"))
            expires_at_dt = _as_utc(datetime.fromisoformat(str(expires_at).replace("Z", "+00:00")))
            should_expire = expires_at_dt <= expiration_cutoff
            expired += int(should_expire)
            if dry_run:
                continue
            connection.execute(
                "UPDATE claims SET expires_at=?,status=CASE WHEN ? THEN 'expired' ELSE status END,"
                "valid_to=CASE WHEN ? AND (valid_to IS NULL OR ?<valid_to) THEN ? "
                "ELSE valid_to END WHERE id=? AND status='active'",
                (
                    expires_at,
                    should_expire,
                    should_expire,
                    expires_at,
                    expires_at,
                    claim["id"],
                ),
            )
        if not dry_run:
            connection.commit()

    return {
        "scanned": scanned,
        "updated": updated,
        "expired": expired,
        "skipped_protected": skipped_protected,
        "dry_run": int(dry_run),
    }


def main() -> None:
    """从命令行执行 expires_at 回填，默认仅预览。"""
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(prog="python -m hl_mem.workers.backfill_expires_at")
    parser.add_argument("--db", default=settings.database_path)
    parser.add_argument("--apply", action="store_true", help="实际写入；省略时为 dry-run")
    parser.add_argument("--batch-size", type=int, default=settings.ttl_backfill_batch_size)
    parser.add_argument("--grace-hours", type=int, default=settings.ttl_backfill_grace_hours)
    args = parser.parse_args()
    database = Database(args.db)
    try:
        result = backfill_expires_at(
            database.open(),
            settings.retention_policy(),
            dry_run=not args.apply,
            batch_size=args.batch_size,
            grace_period=timedelta(hours=args.grace_hours),
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    finally:
        database.close()


if __name__ == "__main__":
    main()
