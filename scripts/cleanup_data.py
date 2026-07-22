"""
hl_mem data cleanup script.

Operations:
1. Restore false disputed → active (old conflict_key pollution)
2. Expire stale temporal claims
3. Deduplicate near-identical claims (keep best, supersede rest)
4. Archive completed plan claims

Safety: --dry-run by default, backup before execution.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path("D:/workspace/hl_agent/hl_mem/var/hl_mem.db")


def get_db():
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    return db


def normalize_text(text: str) -> str:
    """Normalize for comparison: lowercase, strip, collapse whitespace."""
    return " ".join(text.lower().split())


def is_near_duplicate(a: str, b: str) -> bool:
    """Check if two strings are near-duplicates (simple heuristic)."""
    na, nb = normalize_text(a), normalize_text(b)
    if na == nb:
        return True
    # One contains the other
    if len(na) > 20 and na in nb:
        return True
    if len(nb) > 20 and nb in na:
        return True
    # High word overlap
    wa, wb = set(na.split()), set(nb.split())
    if wa and wb:
        overlap = len(wa & wb) / max(len(wa), len(wb))
        if overlap > 0.8 and len(wa) > 3:
            return True
    return False


def analyze(db: sqlite3.Connection) -> dict:
    """Analyze all data quality issues and return action plan."""
    actions = {
        "restore_disputed": [],      # (id, reason)
        "expire_stale": [],          # (id, reason)
        "dedup_supersede": [],       # (keep_id, supersede_id, reason)
        "archive_completed": [],     # (id, reason)
    }

    # === 1. Restore ALL false disputed → active ===
    # Root cause: canonical_attributes like fact.other, plan.other, state.other
    # are generic catch-all types. Different facts with the same generic attr
    # share a conflict_key but are NOT actual conflicts.
    # All 308 disputed claims have conflict_key_version=2 but were disputed
    # by the OLD predicate-only conflict detection.
    generic_attrs = {
        "fact.other", "plan.other", "state.other", "choice.tool",
        "state.service_health", "state.test_suite", "fact.implementation",
        "fact.tool_choice", "config.env", "config.path", "config.other",
        "config.port", "plan.deadline", "identity.other", "preference.tool_choice",
        "fact.other", "choice.database", "config.provider", "config.hardware",
    }
    for row in db.execute(
        "SELECT id, canonical_attribute, conflict_key "
        "FROM claims WHERE status = 'disputed'"
    ).fetchall():
        d = dict(row)
        attr = d["canonical_attribute"]
        # Restore all disputed with generic attrs (they're all false disputes)
        if attr in generic_attrs or ".other" in attr or attr.startswith("plan.") or attr.startswith("state."):
            actions["restore_disputed"].append(
                (d["id"], f"generic attr '{attr}' = false dispute, ck={d['conflict_key'][:8]}")
            )

    # === 2. Expire stale temporal claims ===
    cutoff = (datetime.now() - timedelta(days=7)).isoformat()
    stale_keywords = [
        "events /", "claims", "审计日志", "audit", "active claims",
        "migration 005", "96 events", "202 claims", "passed",
        "个测试", "测试全部通过", "service_health", "deployed with latest",
    ]
    hindsight_keywords = ["hindsight", "Hindsight"]

    for row in db.execute(
        "SELECT id, value_json, canonical_attribute, scope, valid_from "
        "FROM claims WHERE status IN ('active', 'candidate') "
        "AND scope = 'temporal'"
    ).fetchall():
        d = dict(row)
        try:
            v = json.loads(d["value_json"]) if d["value_json"] else {}
            text = str(v).lower()
        except Exception:
            text = (d["value_json"] or "").lower()

        # Stale state snapshots
        is_stale = any(kw.lower() in text for kw in stale_keywords)
        # Hindsight references (retired)
        is_hindsight = any(kw.lower() in text for kw in hindsight_keywords)

        if is_hindsight:
            actions["expire_stale"].append(
                (d["id"], f"hindsight reference (retired)")
            )
        elif is_stale and d["canonical_attribute"].startswith("state."):
            actions["expire_stale"].append(
                (d["id"], f"stale state snapshot: {text[:50]}")
            )
        elif is_stale and "test" in text:
            actions["expire_stale"].append(
                (d["id"], f"stale test result: {text[:50]}")
            )

    # === 3. Deduplicate near-identical active claims ===
    active_claims = []
    for row in db.execute(
        "SELECT id, value_json, canonical_attribute, confidence, valid_from "
        "FROM claims WHERE status IN ('active', 'candidate') "
        "ORDER BY canonical_attribute, confidence DESC"
    ).fetchall():
        d = dict(row)
        try:
            v = json.loads(d["value_json"]) if d["value_json"] else {}
            text = str(v)
        except Exception:
            text = d["value_json"] or ""
        active_claims.append({**d, "text": text})

    # Group by canonical_attribute, find dups within each group
    by_attr = defaultdict(list)
    for c in active_claims:
        by_attr[c["canonical_attribute"]].append(c)

    for attr, claims in by_attr.items():
        if len(claims) < 2:
            continue
        seen = []
        for c in claims:
            is_dup = False
            for s in seen:
                if is_near_duplicate(c["text"], s["text"]):
                    # Keep higher confidence / more recent
                    keep, drop = (s, c) if s["confidence"] >= c["confidence"] else (c, s)
                    if keep["id"] == s["id"]:
                        actions["dedup_supersede"].append(
                            (s["id"], c["id"], f"dup of [{s['id'][:8]}]: {c['text'][:40]}")
                        )
                    else:
                        # c should be kept, s should be superseded — but s was already seen
                        # Remove previous supersede if any, add reverse
                        actions["dedup_supersede"] = [
                            (k, d2, r) for k, d2, r in actions["dedup_supersede"]
                            if d2 != s["id"]
                        ]
                        actions["dedup_supersede"].append(
                            (c["id"], s["id"], f"dup of [{c['id'][:8]}]: {s['text'][:40]}")
                        )
                    is_dup = True
                    break
            if not is_dup:
                seen.append(c)

    # === 4. Archive completed plans ===
    completed_keywords = [
        "清理", "hindsight", "残留", "创建安装脚本", "已完成",
        "m1-m6", "全部完成", "已提交", "已部署", "重启",
    ]
    for row in db.execute(
        "SELECT id, value_json, canonical_attribute, scope "
        "FROM claims WHERE status IN ('active', 'candidate', 'disputed') "
        "AND canonical_attribute LIKE 'plan.%'"
    ).fetchall():
        d = dict(row)
        try:
            v = json.loads(d["value_json"]) if d["value_json"] else {}
            text = str(v).lower()
        except Exception:
            text = (d["value_json"] or "").lower()

        if any(kw.lower() in text for kw in completed_keywords):
            actions["archive_completed"].append(
                (d["id"], f"completed plan: {str(v)[:50]}")
            )

    return actions


def print_dry_run(actions: dict, db: sqlite3.Connection):
    """Print dry-run summary."""
    total = sum(len(v) for v in actions.values())
    print(f"\n{'='*60}")
    print(f"DRY RUN — {total} proposed changes")
    print(f"{'='*60}")

    for op, items in actions.items():
        print(f"\n--- {op} ({len(items)}) ---")
        for item in items[:10]:  # Show first 10
            print(f"  {item}")
        if len(items) > 10:
            print(f"  ... and {len(items)-10} more")

    # Summary stats
    r = db.execute("SELECT status, COUNT(*) as c FROM claims GROUP BY status ORDER BY c DESC").fetchall()
    print(f"\n--- Current status distribution ---")
    for row in r:
        print(f"  {row['status']}: {row['c']}")

    # Projected status after cleanup
    restore_ids = {x[0] for x in actions["restore_disputed"]}
    expire_ids = {x[0] for x in actions["expire_stale"]}
    dedup_drop_ids = {x[1] for x in actions["dedup_supersede"]}
    archive_ids = {x[0] for x in actions["archive_completed"]}

    current = {row["status"]: row["c"] for row in r}
    projected = dict(current)

    # Apply projected changes
    projected["disputed"] = current.get("disputed", 0) - len(restore_ids - expire_ids - archive_ids - dedup_drop_ids)
    projected["active"] = current.get("active", 0) + len(restore_ids & {x[0] for x in []})  # rough

    print(f"\n--- Projected changes ---")
    print(f"  disputed → active: {len(restore_ids)}")
    print(f"  → expired (stale/hindsight): {len(expire_ids)}")
    print(f"  → superseded (dedup): {len(dedup_drop_ids)}")
    print(f"  → expired (completed plans): {len(archive_ids)}")

    total_affected = len(restore_ids | expire_ids | dedup_drop_ids | archive_ids)
    print(f"\n  Total unique claims affected: {total_affected}")
    print(f"  Total claims in DB: {sum(current.values())}")


def execute_cleanup(db: sqlite3.Connection, actions: dict):
    """Execute the cleanup."""
    now = datetime.now().isoformat()
    count = 0

    # 1. Restore false disputed → active
    for claim_id, reason in actions["restore_disputed"]:
        db.execute(
            "UPDATE claims SET status = 'active' WHERE id = ? AND status = 'disputed'",
            (claim_id,)
        )
        count += 1

    # 2. Expire stale temporal
    for claim_id, reason in actions["expire_stale"]:
        db.execute(
            "UPDATE claims SET status = 'expired', expires_at = ? WHERE id = ?",
            (now, claim_id)
        )
        count += 1

    # 3. Dedup supersede
    for keep_id, drop_id, reason in actions["dedup_supersede"]:
        db.execute(
            "UPDATE claims SET status = 'superseded', superseded_by_id = ? WHERE id = ?",
            (keep_id, drop_id)
        )
        count += 1

    # 4. Archive completed plans
    for claim_id, reason in actions["archive_completed"]:
        db.execute(
            "UPDATE claims SET status = 'expired', expires_at = ? WHERE id = ?",
            (now, claim_id)
        )
        count += 1

    db.commit()
    return count


def main():
    parser = argparse.ArgumentParser(description="hl_mem data cleanup")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Dry run (default)")
    parser.add_argument("--execute", action="store_true", help="Actually execute changes")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="Database path")
    args = parser.parse_args()

    print(f"Database: {args.db}")

    db = sqlite3.connect(str(args.db))
    db.row_factory = sqlite3.Row

    # Analyze
    print("Analyzing...")
    actions = analyze(db)

    # Always show dry-run first
    print_dry_run(actions, db)

    if args.execute:
        print(f"\n{'='*60}")
        print("BACKING UP DATABASE...")
        backup_path = args.db.parent / f"hl_mem_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        shutil.copy2(str(args.db), str(backup_path))
        print(f"Backup: {backup_path}")

        print("\nEXECUTING CLEANUP...")
        count = execute_cleanup(db, actions)
        print(f"Done! {count} changes applied.")

        # Final stats
        r = db.execute("SELECT status, COUNT(*) as c FROM claims GROUP BY status ORDER BY c DESC").fetchall()
        print(f"\n--- Final status distribution ---")
        for row in r:
            print(f"  {row['status']}: {row['c']}")
    else:
        print(f"\n(Dry run only. Use --execute to apply changes.)")


if __name__ == "__main__":
    main()
