#!/usr/bin/env python
"""Week 5 integration test — real LLM + real Embedding end-to-end."""
import os, sys, json, hashlib, uuid, time

os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

# Load .env
for line in open('.env'):
    line = line.strip()
    if '=' in line and not line.startswith('#'):
        k, v = line.split('=', 1)
        os.environ[k] = v

os.environ['HL_MEM_DB_PATH'] = 'hl_mem_test.db'
os.environ.setdefault('HL_MEM_EXTRACTOR', 'llm')
os.environ.setdefault('HL_MEM_EMBEDDER', 'real')

sys.path.insert(0, 'src')

# Clean slate
for f in ['hl_mem_test.db', 'hl_mem_budget_test.json']:
    if os.path.exists(f):
        os.remove(f)

from hl_mem.storage.database import Database
from hl_mem.ingest.llm_extractor import LLMExtractor
from hl_mem.ingest.embeddings import Embedder
from hl_mem.ingest.event_filter import EventFilter
from hl_mem.ingest.budget import TokenBudget
from hl_mem.workers.worker import Worker
from hl_mem.storage.repository import EventRepository, ClaimRepository, JobRepository

print("=== HL-Mem Week 5 Integration Test ===")
print(f"Extractor: {os.environ.get('HL_MEM_EXTRACTOR')}")
print(f"Embedder:  {os.environ.get('HL_MEM_EMBEDDER')}")
print()

# Build components
extractor = LLMExtractor(
    os.environ['LLM_API_KEY'],
    os.environ['LLM_BASE_URL'],
    os.environ['LLM_MODEL']
)
embedder = Embedder(
    os.environ['EMBEDDING_API_KEY'],
    os.environ['EMBEDDING_BASE_URL'],
    os.environ['EMBEDDING_MODEL'],
    int(os.environ.get('EMBEDDING_DIM', '2048'))
)
budget = TokenBudget(daily_limit=500000, path='hl_mem_budget_test.json')
event_filter = EventFilter()

# Test events — realistic Chinese conversations
events = [
    {'event_type': 'message', 'actor_type': 'user', 'content': {'text': '我们项目用PostgreSQL，主库在上海'}},
    {'event_type': 'message', 'actor_type': 'user', 'content': {'text': '我喜欢深色模式，浅色太刺眼了'}},
    {'event_type': 'message', 'actor_type': 'user', 'content': {'text': '服务器用的是Ubuntu 22.04'}},
    {'event_type': 'message', 'actor_type': 'user', 'content': {'text': '现在改用浅色模式了，深色看不清代码'}},
    {'event_type': 'explicit_memory', 'actor_type': 'user', 'content': {'text': '记住我的Git用户名是lohr13'}},
    {'event_type': 'message', 'actor_type': 'user', 'content': {'text': '好的，没问题'}},  # should be filtered
]

# Write events
db = Database('hl_mem_test.db')
conn = db.open()
ev_repo = EventRepository(conn)
job_repo = JobRepository(conn)

for i, ev in enumerate(events):
    now = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
    eid = uuid.uuid4().hex
    cj = json.dumps(ev['content'], ensure_ascii=False, sort_keys=True)
    row = {
        'id': eid, 'idempotency_key': f'e2e-{i}',
        'event_type': ev['event_type'], 'actor_type': ev['actor_type'],
        'content_json': cj, 'occurred_at': now, 'recorded_at': now,
        'content_hash': hashlib.sha256(cj.encode()).hexdigest(), 'sensitivity': 'normal',
    }
    created = ev_repo.insert_event(row)
    if created:
        jid = uuid.uuid4().hex
        job_repo.insert_job({
            'id': jid, 'job_type': 'extract_event',
            'payload_json': json.dumps({'event_id': eid}),
            'idempotency_key': f'extract:{eid}',
            'created_at': now, 'updated_at': now,
        })
    status = "→ queued" if created else "(duplicate)"
    print(f"  Event {i+1}: {ev['content']['text'][:35]:35s} {status}")

conn.close()
print()

# Run worker
print("=== Running Worker ===")
worker = Worker('hl_mem_test.db', {
    'extractor': extractor,
    'embedder': embedder,
    'budget': budget,
    'event_filter': event_filter,
})

for i in range(len(events) + 3):
    result = worker.run_once()
    status = result.get('status', '?')
    jtype = result.get('job_type', '')
    detail = result.get('detail', '')
    if status == 'idle':
        print(f"  Queue empty, worker done.")
        break
    extra = f" ({detail})" if detail else ""
    print(f"  Job {i+1}: {jtype:20s} → {status}{extra}")

print()

# Verify claims
conn = Database('hl_mem_test.db').open()
print("=== Claims in DB ===")
rows = conn.execute("SELECT id, predicate, value_json, status, confidence, volatility, namespace_key FROM claims ORDER BY created_at DESC".replace("created_at","recorded_from") if False else "SELECT id, predicate, value_json, status, confidence, volatility, namespace_key FROM claims ORDER BY rowid DESC").fetchall()
for r in rows:
    d = dict(r) if hasattr(r, 'keys') else {}
    val = d.get('value_json', '')
    try:
        val = json.loads(val)
    except:
        pass
    print(f"  [{d.get('status','?'):10s}] {d.get('predicate','?'):15s} = {str(val)[:40]:40s} conf={d.get('confidence',0):.1f} vol={d.get('volatility','?')}")

print()
print("=== FTS Recall Tests ===")
for query in ['PostgreSQL', '深色模式', '浅色模式', 'Ubuntu', 'lohr13', 'Git']:
    claims = ClaimRepository(conn).search_claims_fts(query, 5)
    found = len(claims)
    statuses = [c['status'] for c in claims]
    print(f"  '{query:12s}' → {found} results, statuses={statuses}")

print()
print("=== Budget ===")
stats = budget.get_stats()
print(f"  Used: {stats['used_tokens']} / {stats['daily_limit']} tokens")

print()
print("=== Stats ===")
conn2 = Database('hl_mem_test.db').open()
for row in conn2.execute("SELECT status, count(*) FROM claims GROUP BY status"):
    print(f"  claims.{dict(row)['status']}: {dict(row)['count(*)']}")
for row in conn2.execute("SELECT status, count(*) FROM jobs GROUP BY status"):
    print(f"  jobs.{dict(row)['status']}: {dict(row)['count(*)']}")

conn2.close()
print()
print("=== Integration Test Complete ===")
