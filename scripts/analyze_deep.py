"""Deep dive into fact.other and cross-subject duplicates."""
import sqlite3
import json

conn = sqlite3.connect("var/hl_mem.db")
conn.row_factory = sqlite3.Row

print("=" * 60)
print("Sample fact.other claims (20 random)")
print("=" * 60)
rows = conn.execute(
    "SELECT id, subject_entity_id, predicate, value_json, importance, scope, "
    "valid_from, canonical_slot, topic_tags_json "
    "FROM claims WHERE status='active' AND canonical_attribute='fact.other' "
    "ORDER BY RANDOM() LIMIT 20"
).fetchall()
for r in rows:
    val = ""
    try:
        val = json.loads(r["value_json"])
        if isinstance(val, dict):
            val = val.get("text", str(val))[:80]
        else:
            val = str(val)[:80]
    except Exception:
        val = (r["value_json"] or "")[:80]
    tags = []
    try:
        tags = json.loads(r["topic_tags_json"]) if r["topic_tags_json"] else []
    except Exception:
        pass
    print(f"  [{r['importance']:.1f}] {r['subject_entity_id']} | {r['predicate']} | {val}")
    print(f"       slot={r['canonical_slot']} tags={tags}")

print()
print("=" * 60)
print("Cross-subject duplicate detail")
print("=" * 60)
for dup_val in [
    "hermes-agent/plugins/memory/hl_mem/",
    "disputed",
    "0.1s",
    "coding plan",
]:
    rows = conn.execute(
        "SELECT id, subject_entity_id, predicate, value_json, importance, scope, canonical_attribute "
        "FROM claims WHERE status='active' AND value_json LIKE ? "
        "ORDER BY subject_entity_id",
        (f"%{dup_val}%",),
    ).fetchall()
    if len(rows) > 1:
        print(f"\n--- '{dup_val}' ({len(rows)} claims) ---")
        for r in rows:
            val = ""
            try:
                val = json.loads(r["value_json"])
                if isinstance(val, dict):
                    val = val.get("text", str(val))[:100]
                else:
                    val = str(val)[:100]
            except Exception:
                val = (r["value_json"] or "")[:100]
            print(f"  [{r['subject_entity_id']}] {r['predicate']} | attr={r['canonical_attribute']} | {val}")

print()
print("=" * 60)
print("Expired+Superseded: any with unique info not in active?")
print("=" * 60)
# Count expired/superseded that have no active counterpart
dead_with_no_active = conn.execute(
    "SELECT COUNT(*) FROM claims c1 WHERE c1.status IN ('expired','superseded') "
    "AND NOT EXISTS ("
    "  SELECT 1 FROM claims c2 WHERE c2.status='active' "
    "  AND c2.subject_entity_id = c1.subject_entity_id "
    "  AND c2.predicate = c1.predicate"
    ")"
).fetchone()[0]
total_dead = conn.execute(
    "SELECT COUNT(*) FROM claims WHERE status IN ('expired','superseded')"
).fetchone()[0]
print(f"  Dead claims with no active counterpart on same subject+predicate: {dead_with_no_active}")
print(f"  Total dead: {total_dead}")

print()
print("=" * 60)
print("Same-subject duplicate detail (hl_mem + fact, top 10)")
print("=" * 60)
rows = conn.execute(
    "SELECT id, subject_entity_id, predicate, value_json, importance, valid_from, canonical_attribute, canonical_slot "
    "FROM claims WHERE status='active' AND subject_entity_id='hl_mem' AND predicate='事实' "
    "ORDER BY valid_from DESC LIMIT 10"
).fetchall()
for r in rows:
    val = ""
    try:
        val = json.loads(r["value_json"])
        if isinstance(val, dict):
            val = val.get("text", str(val))[:80]
        else:
            val = str(val)[:80]
    except Exception:
        val = (r["value_json"] or "")[:80]
    print(f"  [{r['valid_from'][:10]}] imp={r['importance']:.1f} attr={r['canonical_attribute']} | {val}")

conn.close()
