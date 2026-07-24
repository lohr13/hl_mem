"""Test embedding for Chinese text using the same env as server."""
import os
import sys

# Load .env like start_server.py does
from pathlib import Path
env_file = Path(__file__).parent.parent / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

os.environ.setdefault("HL_MEM_RERANKER", "on")
os.environ.setdefault("HL_MEM_EMBEDDER", "real")

from hl_mem.settings import Settings
from hl_mem.components import make_embedder
from hl_mem.ingest.embedder import pack_vector, unpack_vector
from hl_mem.core.vector import cosine_similarity
import struct

settings = Settings.from_env()
print(f"embedder_mode: {settings.embedder_mode}")
print(f"embedding_model: {settings.embedding_model}")
print(f"embedding_dim: {settings.embedding_dim}")
print(f"base_url: {settings.embedding_base_url}")

embedder = make_embedder(settings)
print(f"\nEmbedder type: {type(embedder).__name__}")

for query in ["hl_mem", "唇形同步", "配置", "memory"]:
    print(f"\n--- Query: '{query}' ---")
    try:
        blob = embedder.embed_one(query)
        print(f"  Type: {type(blob).__name__}, Length: {len(blob)} bytes")
        vec = unpack_vector(blob)
        print(f"  Vector dim: {len(vec)}")
        print(f"  First 5: {vec[:5]}")
        print(f"  Norm: {sum(v*v for v in vec)**0.5:.4f}")
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")

# Now test dense search with the Chinese embedding
print("\n\n=== Dense search test ===")
import sqlite3, json
conn = sqlite3.connect("var/hl_mem.db")
conn.row_factory = sqlite3.Row

from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
db = Database("var/hl_mem.db")
conn2 = db.open()
repo = ClaimRepository(conn2)

for query in ["唇形同步", "配置", "hl_mem"]:
    print(f"\n--- '{query}' ---")
    try:
        blob = embedder.embed_one(query)
        results = repo.search_claims_vector(blob, 5, None, None, None, namespace="default")
        print(f"  Dense results: {len(results)}")
        for r in results[:3]:
            val = ""
            try:
                v = json.loads(r.get("value_json", "{}"))
                val = v.get("text", str(v))[:60] if isinstance(v, dict) else str(v)[:60]
            except Exception:
                pass
            print(f"    {r['id'][:12]} {r.get('subject_entity_id','?')} | {val}")
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")

conn.close()
conn2.close()
