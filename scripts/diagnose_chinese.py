"""Diagnose Chinese recall issues."""
import sqlite3
import json

# 1. FTS test
conn = sqlite3.connect("var/hl_mem.db")
conn.row_factory = sqlite3.Row

print("=== FTS raw test: 唇形 ===")
try:
    rows = conn.execute("SELECT id FROM claims_fts WHERE claims_fts MATCH ? LIMIT 3", ("唇形",)).fetchall()
    print(f"FTS hits: {len(rows)}")
except Exception as e:
    print(f"FTS error: {e}")

print("\n=== FTS raw test: hl_mem ===")
try:
    rows = conn.execute("SELECT id FROM claims_fts WHERE claims_fts MATCH ? LIMIT 3", ("hl_mem",)).fetchall()
    print(f"FTS hits: {len(rows)}")
except Exception as e:
    print(f"FTS error: {e}")

print("\n=== FTS raw test: Codex ===")
try:
    rows = conn.execute("SELECT id FROM claims_fts WHERE claims_fts MATCH ? LIMIT 3", ("Codex",)).fetchall()
    print(f"FTS hits: {len(rows)}")
except Exception as e:
    print(f"FTS error: {e}")

conn.close()

# 2. Dense vector search test
print("\n=== Dense vector search test ===")
from hl_mem.storage.database import Database
from hl_mem.storage.claims import ClaimRepository
from hl_mem.components import make_embedder
from hl_mem.settings import Settings

settings = Settings.from_env()
embedder = make_embedder(settings)
db = Database("var/hl_mem.db")
conn2 = db.open()
repo = ClaimRepository(conn2)

for query in ["唇形同步", "代理配置", "记忆系统架构", "hl_mem"]:
    raw_vec = embedder.embed(query)
    if raw_vec is None:
        print(f"\n'{query}': Embedding returned None!")
        continue
    # Pack to bytes if it's a list
    import struct
    if isinstance(raw_vec, list):
        blob = struct.pack(f"<{len(raw_vec)}f", *raw_vec)
    else:
        blob = raw_vec
    results = repo.search_claims_vector(blob, 5, None, None, None, namespace="default")
    print(f"\nDense hits for '{query}': {len(results)}")
    for r in results[:3]:
        val = ""
        try:
            v = json.loads(r.get("value_json", "{}"))
            val = v.get("text", str(v))[:60] if isinstance(v, dict) else str(v)[:60]
        except Exception:
            pass
        print(f"  {r['id'][:12]} {r.get('subject_entity_id','?')} | {val}")

conn2.close()
