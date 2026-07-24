"""
hl_mem 数据清洗脚本 v2
执行前自动备份，执行后输出统计。

用法：
  python scripts/cleanup_data_v2.py --dry-run   # 只看不改
  python scripts/cleanup_data_v2.py              # 实际执行
"""
import sqlite3
import json
import sys
import os
from datetime import datetime, timezone
from collections import Counter

DB_PATH = "var/hl_mem.db"
DRY_RUN = "--dry-run" in sys.argv


def log(msg):
    prefix = "[DRY] " if DRY_RUN else "[EXEC] "
    print(f"{prefix}{msg}")


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # ══════════════════════════════════════════════════════════════
    # P0: Purge 死数据
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("P0: Purge 死数据 (expired + superseded)")
    print("=" * 60)

    # 0a. Count before
    expired_cnt = conn.execute("SELECT COUNT(*) FROM claims WHERE status='expired'").fetchone()[0]
    superseded_cnt = conn.execute("SELECT COUNT(*) FROM claims WHERE status='superseded'").fetchone()[0]
    print(f"Before: expired={expired_cnt}, superseded={superseded_cnt}, total_dead={expired_cnt + superseded_cnt}")

    if not DRY_RUN:
        # Disable FK during cleanup (we'll clean in dependency order)
        conn.execute("PRAGMA foreign_keys = OFF")

        # Delete evidence_links pointing to dead claims (column is derived_id)
        r = conn.execute(
            "DELETE FROM evidence_links WHERE derived_id IN "
            "(SELECT id FROM claims WHERE status IN ('expired', 'superseded'))"
        )
        log(f"Deleted {r.rowcount} evidence_links pointing to dead claims")

        # Delete dedup_pairs referencing dead claims
        r = conn.execute(
            "DELETE FROM dedup_pairs WHERE left_claim_id IN "
            "(SELECT id FROM claims WHERE status IN ('expired', 'superseded')) "
            "OR right_claim_id IN "
            "(SELECT id FROM claims WHERE status IN ('expired', 'superseded'))"
        )
        log(f"Deleted {r.rowcount} dedup_pairs referencing dead claims")

        # Delete consolidation_pairs referencing dead claims
        r = conn.execute(
            "DELETE FROM consolidation_pairs WHERE left_claim_id IN "
            "(SELECT id FROM claims WHERE status IN ('expired', 'superseded')) "
            "OR right_claim_id IN "
            "(SELECT id FROM claims WHERE status IN ('expired', 'superseded'))"
        )
        log(f"Deleted {r.rowcount} consolidation_pairs referencing dead claims")

        # Delete conflict_cases referencing dead claims
        r = conn.execute(
            "DELETE FROM conflict_cases WHERE left_claim_id IN "
            "(SELECT id FROM claims WHERE status IN ('expired', 'superseded')) "
            "OR right_claim_id IN "
            "(SELECT id FROM claims WHERE status IN ('expired', 'superseded'))"
        )
        log(f"Deleted {r.rowcount} conflict_cases referencing dead claims")

        # Null out self-referencing FKs on active claims
        r = conn.execute(
            "UPDATE claims SET supersedes_id=NULL WHERE supersedes_id IN "
            "(SELECT id FROM claims WHERE status IN ('expired', 'superseded'))"
        )
        log(f"Nulled {r.rowcount} supersedes_id references")
        r = conn.execute(
            "UPDATE claims SET superseded_by_id=NULL WHERE superseded_by_id IN "
            "(SELECT id FROM claims WHERE status IN ('expired', 'superseded'))"
        )
        log(f"Nulled {r.rowcount} superseded_by_id references")

        # Delete FTS index entries for dead claims
        try:
            r = conn.execute(
                "DELETE FROM claims_fts WHERE id IN "
                "(SELECT id FROM claims WHERE status IN ('expired', 'superseded'))"
            )
            log(f"Deleted {r.rowcount} FTS entries for dead claims")
        except sqlite3.OperationalError:
            pass

        # Delete the dead claims themselves
        r = conn.execute("DELETE FROM claims WHERE status IN ('expired', 'superseded')")
        log(f"Deleted {r.rowcount} dead claims")

        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")

    # Verify
    remaining_expired = conn.execute("SELECT COUNT(*) FROM claims WHERE status='expired'").fetchone()[0]
    remaining_superseded = conn.execute("SELECT COUNT(*) FROM claims WHERE status='superseded'").fetchone()[0]
    remaining_active = conn.execute("SELECT COUNT(*) FROM claims WHERE status='active'").fetchone()[0]
    log(f"After: expired={remaining_expired}, superseded={remaining_superseded}, active={remaining_active}")

    # ══════════════════════════════════════════════════════════════
    # P1: 重分类 fact.other
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("P1: 重分类 fact.other → 更细分类")
    print("=" * 60)

    # Keyword-based reclassification rules
    RECLASSIFY_RULES = [
        # (keywords_in_value, new_attribute)
        (["分层架构", "application/", "core/", "domain/", "storage/", "api/", "adapters/",
          "IngestService", "RecallService", "ForgetService", "层架构", "分层",
          "模块结构", "目录结构", "顶层分层"], "fact.architecture"),
        (["重构完成", "6阶段", "Phase 1", "Phase 2", "Phase 3", "Phase 4", "Phase 5",
          "Phase 6", "Phase 7", "Phase 8", "Phase 9", "Phase 10", "Phase 11",
          "Phase 12", "Phase 13", "Phase 14", "Phase 15", "Phase 16", "Phase 17",
          "Phase 18", "阶段重构", "批次", "全部完成", "全部修复", "5批次",
          "commit=", "push"], "fact.history"),
        (["不引入", "不用", "暂不", "跳过", "选择", "决定不用", "改用",
          "决定", "务实选择", "当前不需要", "暂缓", "推迟"], "fact.decision"),
        (["CLI", "--version", "healthz", "端点", "支持", "能力",
          "可以", "能够", "支持查询", "支持配置"], "fact.capability"),
        (["token budget", "锁", "竞态", "TOCTOU", "lost update", "race condition",
          "bug", "问题", "缺陷", "僵尸进程", "崩溃"], "fact.issue"),
        (["接口", "REST", "API", "endpoint", "HTTP", "schema",
          "OpenAPI"], "fact.api_design"),
        (["worker", "异步", "队列", "job", "定时", "04:00"], "fact.worker"),
    ]

    fact_others = conn.execute(
        "SELECT id, value_json, topic_tags_json FROM claims "
        "WHERE status='active' AND canonical_attribute='fact.other'"
    ).fetchall()

    reclassified = Counter()
    for row in fact_others:
        text = ""
        try:
            val = json.loads(row["value_json"])
            if isinstance(val, dict):
                text = val.get("text", str(val))
            else:
                text = str(val)
        except Exception:
            text = row["value_json"] or ""

        text_lower = text.lower()
        new_attr = None
        for keywords, attr in RECLASSIFY_RULES:
            if any(kw.lower() in text_lower for kw in keywords):
                new_attr = attr
                break

        if new_attr:
            reclassified[new_attr] += 1
            if not DRY_RUN:
                # Update topic_tags too
                tags = []
                try:
                    tags = json.loads(row["topic_tags_json"]) if row["topic_tags_json"] else []
                except Exception:
                    pass
                new_tags = list(set(tags + new_attr.split(".")[1:]))
                conn.execute(
                    "UPDATE claims SET canonical_attribute=?, topic_tags_json=? WHERE id=?",
                    (new_attr, json.dumps(new_tags), row["id"]),
                )

    print(f"Reclassified {sum(reclassified.values())} / {len(fact_others)} fact.other claims:")
    for attr, cnt in reclassified.most_common():
        print(f"  → {attr}: {cnt}")

    remaining_other = len(fact_others) - sum(reclassified.values())
    print(f"  Remaining fact.other: {remaining_other}")

    if not DRY_RUN:
        conn.commit()

    # ══════════════════════════════════════════════════════════════
    # P2: 跨 subject 去重 — 合并完全相同的 value
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("P2: 跨 subject 去重 (相同 value_json 的合并)")
    print("=" * 60)

    dupes = conn.execute(
        "SELECT value_json, COUNT(*) as cnt, "
        "GROUP_CONCAT(id) as ids, "
        "GROUP_CONCAT(subject_entity_id) as subjects "
        "FROM claims WHERE status='active' AND length(value_json) > 30 "
        "GROUP BY value_json HAVING cnt > 1 "
        "ORDER BY cnt DESC"
    ).fetchall()

    merged_cnt = 0
    for d in dupes:
        ids = d["ids"].split(",")
        subjects = d["subjects"].split(",")
        # Keep the first (highest importance or most general subject), supersede the rest
        # Prefer subject "用户" or "hl_mem" as canonical
        rows_info = []
        for cid in ids:
            r = conn.execute(
                "SELECT id, subject_entity_id, importance, canonical_attribute FROM claims WHERE id=?",
                (cid,),
            ).fetchone()
            if r:
                rows_info.append(dict(r))

        # Sort: prefer 用户 > hl_mem > others; then by importance desc
        subject_priority = {"用户": 0, "hl_mem": 1, "Hermes": 2}
        rows_info.sort(
            key=lambda x: (subject_priority.get(x["subject_entity_id"], 99), -x["importance"])
        )

        keeper = rows_info[0]
        to_supersede = rows_info[1:]

        val_preview = ""
        try:
            val = json.loads(d["value_json"])
            if isinstance(val, dict):
                val_preview = val.get("text", str(val))[:60]
            else:
                val_preview = str(val)[:60]
        except Exception:
            val_preview = (d["value_json"] or "")[:60]

        print(f"  [{d['cnt']}x] keeper={keeper['subject_entity_id']} | supersede {[r['subject_entity_id'] for r in to_supersede]}")
        print(f"       {val_preview}")

        if not DRY_RUN:
            for r in to_supersede:
                conn.execute(
                    "UPDATE claims SET status='superseded', superseded_by_id=? WHERE id=?",
                    (keeper["id"], r["id"]),
                )
            merged_cnt += len(to_supersede)

    if not DRY_RUN:
        conn.commit()
    log(f"Merged (superseded) {merged_cnt} cross-subject duplicates")

    # ══════════════════════════════════════════════════════════════
    # P3: 同 subject+predicate 语义合并（保守版：只合并明显重复）
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("P3: 同 subject+predicate 合并 (高相似度)")
    print("=" * 60)

    # Find same subject+predicate with very similar values
    groups = conn.execute(
        "SELECT subject_entity_id, predicate, COUNT(*) as cnt, "
        "GROUP_CONCAT(id) as ids "
        "FROM claims WHERE status='active' "
        "GROUP BY subject_entity_id, predicate HAVING cnt > 3 "
        "ORDER BY cnt DESC LIMIT 10"
    ).fetchall()

    p3_merged = 0
    for g in groups:
        ids = g["ids"].split(",")
        rows = []
        for cid in ids:
            r = conn.execute(
                "SELECT id, value_json, importance, valid_from, canonical_attribute FROM claims WHERE id=?",
                (cid,),
            ).fetchone()
            if r:
                rows.append(dict(r))

        # Deduplicate by exact value_json match within same subject+predicate
        seen_values = {}
        for r in rows:
            val = r["value_json"] or ""
            if val in seen_values:
                # This is an exact duplicate, supersede it
                keeper_id = seen_values[val]
                if not DRY_RUN:
                    conn.execute(
                        "UPDATE claims SET status='superseded', superseded_by_id=? WHERE id=?",
                        (keeper_id, r["id"]),
                    )
                p3_merged += 1
            else:
                seen_values[val] = r["id"]

    print(f"Exact-duplicate merge within same subject+predicate: {p3_merged}")
    if not DRY_RUN:
        conn.commit()

    # ══════════════════════════════════════════════════════════════
    # Final stats
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("Final Statistics")
    print("=" * 60)

    for r in conn.execute("SELECT status, COUNT(*) as cnt FROM claims GROUP BY status ORDER BY cnt DESC"):
        print(f"  {r['status']}: {r['cnt']}")

    total = conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
    print(f"  TOTAL: {total}")

    print()
    print("Active by canonical_attribute (top 15):")
    for r in conn.execute(
        "SELECT canonical_attribute, COUNT(*) as cnt FROM claims WHERE status='active' "
        "GROUP BY canonical_attribute ORDER BY cnt DESC LIMIT 15"
    ):
        print(f"  {r['canonical_attribute']}: {r['cnt']}")

    fact_other = conn.execute(
        "SELECT COUNT(*) FROM claims WHERE status='active' AND canonical_attribute='fact.other'"
    ).fetchone()[0]
    total_active = conn.execute("SELECT COUNT(*) FROM claims WHERE status='active'").fetchone()[0]
    if total_active > 0:
        print(f"\n  fact.other ratio: {fact_other}/{total_active} = {fact_other/total_active*100:.1f}%")

    conn.close()
    print(f"\n{'DRY RUN' if DRY_RUN else 'COMPLETED'} — backup at var/hl_mem_backup_*.db")


if __name__ == "__main__":
    main()
