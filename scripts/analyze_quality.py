"""Quick data quality analysis for hl_mem claims."""
import sqlite3
import json
from collections import Counter

conn = sqlite3.connect("var/hl_mem.db")
conn.row_factory = sqlite3.Row

print("=" * 60)
print("Claims by Status")
print("=" * 60)
for r in conn.execute("SELECT status, COUNT(*) as cnt FROM claims GROUP BY status ORDER BY cnt DESC"):
    print(f"  {r['status']}: {r['cnt']}")

print()
print("=" * 60)
print("Claims by Scope")
print("=" * 60)
for r in conn.execute("SELECT scope, COUNT(*) as cnt FROM claims GROUP BY scope ORDER BY cnt DESC"):
    print(f"  {r['scope']}: {r['cnt']}")

print()
print("=" * 60)
print("Active Claims by Canonical Attribute (top 20)")
print("=" * 60)
for r in conn.execute(
    "SELECT canonical_attribute, COUNT(*) as cnt FROM claims WHERE status='active' "
    "GROUP BY canonical_attribute ORDER BY cnt DESC LIMIT 20"
):
    print(f"  {r['canonical_attribute'] or 'NULL'}: {r['cnt']}")

print()
# fact.other ratio
total_active = conn.execute("SELECT COUNT(*) FROM claims WHERE status='active'").fetchone()[0]
fact_other = conn.execute(
    "SELECT COUNT(*) FROM claims WHERE status='active' AND canonical_attribute='fact.other'"
).fetchone()[0]
print(f"fact.other ratio: {fact_other}/{total_active} = {fact_other/total_active*100:.1f}%")

print()
print("=" * 60)
print("Topic Tags Coverage")
print("=" * 60)
with_tags = conn.execute(
    "SELECT COUNT(*) FROM claims WHERE topic_tags_json IS NOT NULL AND topic_tags_json != '[]'"
).fetchone()[0]
print(f"With tags: {with_tags}/{total_active} active")

print()
print("=" * 60)
print("Importance Distribution (active only)")
print("=" * 60)
for r in conn.execute(
    "SELECT CASE WHEN importance < 0.3 THEN 'low(<0.3)' "
    "WHEN importance < 0.7 THEN 'mid(0.3-0.7)' "
    "ELSE 'high(>=0.7)' END as band, COUNT(*) as cnt "
    "FROM claims WHERE status='active' GROUP BY band ORDER BY cnt DESC"
):
    print(f"  {r['band']}: {r['cnt']}")

print()
print("=" * 60)
print("Potential Duplicates (same subject_entity+predicate, active)")
print("=" * 60)
dupes = conn.execute(
    "SELECT subject_entity_id, predicate, COUNT(*) as cnt "
    "FROM claims WHERE status='active' "
    "GROUP BY subject_entity_id, predicate HAVING cnt > 1 "
    "ORDER BY cnt DESC LIMIT 15"
).fetchall()
for r in dupes:
    print(f"  [{r['cnt']}x] {r['subject_entity_id'] or 'NULL'} | {r['predicate']}")

print()
print("=" * 60)
print("Low Importance Active Claims (importance < 0.2)")
print("=" * 60)
low = conn.execute(
    "SELECT id, subject_entity_id, predicate, value_json, importance, canonical_attribute "
    "FROM claims WHERE status='active' AND importance < 0.2 "
    "ORDER BY importance LIMIT 20"
).fetchall()
for r in low:
    import json as _json
    val = ""
    try:
        val = str(_json.loads(r["value_json"]))[:60] if r["value_json"] else ""
    except Exception:
        val = (r["value_json"] or "")[:60]
    print(f"  [{r['importance']:.2f}] {r['subject_entity_id'] or 'NULL'} | {r['predicate']} | {val}")

print(f"\n  Total low-importance active: {len(low)} (showing first 20)")

print()
print("=" * 60)
print("Temporal Claims That Should Have Expired")
print("=" * 60)
# temporal scope with recorded_at older than 14 days
stale_temporal = conn.execute(
    "SELECT id, subject_entity_id, predicate, value_json, importance, recorded_from "
    "FROM claims WHERE status='active' AND scope='temporal' "
    "AND recorded_from < '2026-07-10' "
    "ORDER BY recorded_from LIMIT 15"
).fetchall()
for r in stale_temporal:
    import json as _json
    val = ""
    try:
        val = str(_json.loads(r["value_json"]))[:60] if r["value_json"] else ""
    except Exception:
        val = (r["value_json"] or "")[:60]
    print(f"  [{r['importance']:.2f}] {r['recorded_from'][:10] if r['recorded_from'] else 'N/A'} | {r['subject_entity_id'] or 'NULL'} | {r['predicate']} | {val}")
print(f"\n  Total stale temporal: {len(stale_temporal)} (showing first 15)")

print()
print("=" * 60)
print("Expired/Superseded Claims Still Occupying Space")
print("=" * 60)
expired = conn.execute("SELECT COUNT(*) FROM claims WHERE status='expired'").fetchone()[0]
superseded = conn.execute("SELECT COUNT(*) FROM claims WHERE status='superseded'").fetchone()[0]
print(f"  expired: {expired}")
print(f"  superseded: {superseded}")
print(f"  total dead: {expired + superseded}")

print()
print("=" * 60)
print("Canonical Slot Coverage")
print("=" * 60)
with_slot = conn.execute(
    "SELECT COUNT(*) FROM claims WHERE status='active' AND canonical_slot IS NOT NULL"
).fetchone()[0]
print(f"  With slot: {with_slot}/{total_active} active")
slot_dist = conn.execute(
    "SELECT canonical_slot, COUNT(*) as cnt FROM claims WHERE status='active' "
    "AND canonical_slot IS NOT NULL GROUP BY canonical_slot ORDER BY cnt DESC LIMIT 15"
).fetchall()
for r in slot_dist:
    print(f"    {r['canonical_slot']}: {r['cnt']}")

print()
print("=" * 60)
print("Cross-Subject Duplicates (same object text, different subject)")
print("=" * 60)
xs_dups = conn.execute(
    "SELECT value_json, COUNT(DISTINCT subject_entity_id) as subj_cnt, COUNT(*) as claim_cnt "
    "FROM claims WHERE status='active' AND length(value_json) > 20 "
    "GROUP BY value_json HAVING subj_cnt > 1 "
    "ORDER BY claim_cnt DESC LIMIT 10"
).fetchall()
for r in xs_dups:
    import json as _json
    val = ""
    try:
        val = str(_json.loads(r["value_json"]))[:80] if r["value_json"] else ""
    except Exception:
        val = (r["value_json"] or "")[:80]
    print(f"  [{r['claim_cnt']} claims, {r['subj_cnt']} subjects] {val}")

conn.close()
