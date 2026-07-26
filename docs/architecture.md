# HL-Mem Architecture

- Document baseline: v0.13.2
- Updated: 2026-07-26
- Deployment baseline: local-first, SQLite-first

This document describes the shipped architecture. Feature maturity and default modes are tracked in the
[capability matrix](capability-matrix.md); future work is kept in [proposals](proposals/) and the
[implementation plan](implementation-plan.md).

## 1. System Boundary

HL-Mem turns Agent events into durable, evidence-backed context. It does not replace the active conversation window,
execute external tools, store general model knowledge, or train the host Agent. REST, MCP, and Hermes clients submit
events or recall queries and receive structured memory with temporal and provenance metadata.

SQLite WAL is the supported primary store. FTS5 provides lexical retrieval and vector embeddings are stored as BLOBs for
bounded brute-force cosine search. External LLM, embedding, reranking, and vision providers are optional capabilities
injected through settings; they are not storage dependencies.

## 2. Layered Architecture

```mermaid
flowchart TB
    C[REST / MCP / Hermes clients] --> A[Adapters: api / mcp / adapters.hermes]
    A --> S[Application services: ingest / recall / forget]
    A --> X[Experience service: episode / trace / policy]
    S --> D[Domain + Core]
    X --> D
    S --> I[Ingest: filter / extract / embed]
    S --> R[Recall: FTS / dense / tag / relation / rerank]
    W[Workers: TTL / decay / consolidate / deduplicate / derive] --> S
    I --> L[External model providers]
    R --> L
    W --> L
    S --> P[Storage repositories]
    X --> P
    W --> P
    P --> DB[(SQLite WAL + FTS5 + vector BLOBs)]
    O[Audit logs + LLM spans] --> DB
    E[Offline evaluation + LongMemEval] --> S
```

The executable import gate in `scripts/check_imports.py` protects the dependency direction: `core` has no infrastructure
dependencies, and `domain` does not import `storage`, `api`, or `workers`. Adapters translate protocols; application
services own use cases and transaction boundaries; repositories own persistence.

## 3. Code Structure

```text
src/hl_mem/
├── adapters/hermes/          # Hermes provider, client, episode mapping, thin plugin delegate
├── api/
│   ├── server.py             # FastAPI assembly, middleware, exception mapping, 16 REST routes
│   └── schemas.py            # Pydantic request and response contracts
├── application/
│   ├── ingest.py             # IngestService and atomic Claim write path
│   ├── recall.py             # RecallService orchestration and context packing
│   └── forget.py             # ForgetService and cascading withdrawal
├── core/
│   └── vector.py             # Pure cosine-similarity math
├── domain/
│   ├── claims/               # Claim model, conflict, dedup, retention, slot/tag query logic
│   ├── content.py            # Multimodal content protocol
│   ├── entity.py             # Entity normalization
│   ├── recall.py             # Recall intents and domain rules
│   ├── relations.py          # Memory relationship model
│   └── temporal.py           # Dual-time visibility
├── evaluation/               # Benchmarks, metrics, reports, LongMemEval adapter
├── experience/
│   └── service.py            # Episode, Trace, reward, Policy operations
├── ingest/
│   ├── budget.py             # Daily token budget
│   ├── chunking.py           # Structure-aware chunking
│   ├── embedder.py           # Fake/real embedding implementations
│   ├── event_filter.py       # Event value filtering
│   ├── extractors.py         # Fake/LLM extractor interface
│   └── llm_extractor.py      # Prompted structured extraction
├── llm/
│   ├── client.py             # Provider-independent LLM client
│   ├── providers.py          # Bailian, Zhipu, OpenAI-compatible providers
│   └── types.py              # LLM request/response types
├── mcp/
│   └── server.py             # Five-tool MCP contract
├── observability/            # Audit events and persistent LLM call spans
├── recall/
│   ├── observation.py        # Derived-memory assembly
│   ├── ranking.py            # Multi-factor ranking
│   ├── relation_expansion.py # One-hop relation expansion
│   ├── reranker.py           # Optional reranking (model configured via .env)
│   ├── staged_pipeline.py    # FTS + dense + optional tag channel and RRF
│   └── trace.py              # SearchTrace diagnostics and metrics
├── security/                 # Retention and content policy
├── storage/
│   ├── backup.py             # Online SQLite backup
│   ├── claims.py             # Claim repository
│   ├── database.py           # Connection management and migration runner
│   ├── events.py             # Immutable Event repository
│   ├── evidence.py           # Evidence links
│   ├── experience.py         # Episode/Trace/Policy repository
│   ├── jobs.py               # Durable job queue
│   ├── relation_proposals.py # Auditable relation candidates
│   ├── usefulness.py         # Feedback usefulness aggregation
│   └── migrations/           # 29 immutable SQL migrations (001–029)
├── workers/
│   ├── worker.py             # Job leasing, dispatch, progress, heartbeat
│   ├── ttl.py                # Importance-aware expiry
│   ├── decay.py              # Confidence decay and archival
│   ├── consolidate.py        # LLM semantic consolidation
│   ├── deduplicate.py        # Cross-subject semantic deduplication
│   ├── backfill_expires_at.py# TTL backfill utility
│   ├── discover_relations.py # Relation proposal discovery
│   ├── mental_models.py      # Mental Model maintenance
│   ├── rebuild_usefulness.py # Usefulness rebuild
│   └── induce_policies.py    # Experience-to-Policy induction
├── components.py             # Central component factories and health state
├── config.py                 # Shared constants
├── errors.py                 # Application exception family
├── http_utils.py             # Retry and timeout utilities
├── lifecycle.py              # Central state-transition guards
├── protocols.py              # Backend and component protocols
├── settings.py               # Environment-backed settings and validation
└── cli.py                    # Maintenance, backup, import/export, evaluation CLI
```

## 4. Memory Model

HL-Mem uses two event-sourced channels:

| Type | Role | Persistence |
|---|---|---|
| Event | Immutable source input and idempotency boundary | `events` |
| Claim | Atomic fact extracted from evidence | `claims` |
| Observation / Mental Model | Stable knowledge derived from multiple Claims | `derivations` |
| Episode | One continuous task experience | `episodes` |
| Trace | Tool call or action inside an Episode | `traces` |
| Policy / Procedure | Reusable guidance induced from experience | `policies` |

Claims carry `valid_from`/`valid_to` for business validity and `recorded_from`/`recorded_to` for database knowledge time.
This supports both “what was true then?” and “what did the system know then?” Evidence links connect Claims to source
Events. Derivations retain source relations and become stale when a source Claim is withdrawn.

Classification combines a controlled operational slot with open topic tags. Slots drive conflict, retention, and
preference behavior; tags improve discovery without becoming hard schema.

## 5. Write Pipeline

```text
Client
  → POST /v1/events
  → idempotent Event insert + durable extract_event Job
  → Worker lease and EventFilter / optional deterministic pre-filter
  → LLM extraction with preceding context and temporal anchoring
  → fact_hash v2 exact deduplication
  → canonical attribute + conflict_key deterministic conflict resolution
  → LLM four-way consolidation for gray-zone conflicts
  → best-match semantic deduplication (configured threshold; default 0.82)
  → entity normalization + slot/tags + retention/expiry calculation
  → embedding generation and evidence links
  → Claim commit
```

The Claim mutation sequence—status update, Claim insert, supersede operation, and evidence link—runs in one
`BEGIN IMMEDIATE` transaction. External calls use configured timeouts and retries; errors are recorded rather than
silently swallowed. Idempotent retries return the original Event instead of duplicating work.

Observation and Mental Model derivation is a separate maintenance path, not part of the Claim write transaction. The
mental-model worker evaluates active evidence after ingestion and writes or refreshes derivations when its evidence rules
are satisfied.

Explicit `POST /v1/memories` writes a pinned memory through the same application boundary. The experience channel writes
Episodes and Traces separately, accepts reward/outcome feedback, and lets workers induce reusable Policies.

## 6. Recall Pipeline

```text
POST /v1/recall
  → RecallIntent routing and optional bounded query expansion
  → namespace / subject / dual-time visibility filters
  → FTS5 BM25 + dense cosine + optional tag candidates
  → reciprocal-rank fusion (RRF)
  → recency + importance + access + scope + helpfulness scoring
  → optional reranking (model configured via .env)
  → relation, Observation, and Experience expansion
  → token-budget and cross-type quota packing
  → evidence-aware Context Packet + optional SearchTrace
```

Lexical retrieval uses trigram tokenization for Claim/tag Chinese substring behavior; Event FTS remains `unicode61`.
Dense retrieval scans a configured candidate bound before scoring. Optional provider failures degrade to deterministic or
original-query paths so the SQLite retrieval core remains available.

## 7. Conflict, Deduplication, and Lifecycle

The write path applies progressively more expensive checks: bounded JSON `fact_hash` equality, canonical-attribute
`conflict_key`, then semantic similarity. Deterministic mutual-exclusion rules take priority; ambiguous cases can enter the
audited conflict consolidator. Conflict cases converge through guarded terminal states.

Retention is a pure function of scope and importance. Ephemeral memories expire; temporal and permanent memories decay on
different schedules; access and sufficiently supported helpful feedback can extend useful life within configured caps.
Archival clears embeddings. Explicit forgetting withdraws the Claim, clears its vector, preserves audit history, and marks
dependent derivations stale.

## 8. Interfaces

| Interface | Responsibility | Maturity |
|---|---|---|
| REST / FastAPI | Full ingestion, recall, experience, feedback, jobs, and health surface | Stable |
| Hermes Provider | Agent-facing memory adapter with timeout and circuit breaker | Stable |
| MCP | Minimal five-tool memory contract | Beta |
| CLI | Worker, maintenance, backup, import/export, benchmark operations | Stable |

### REST route map

| Method | Path | Application responsibility |
|---|---|---|
| `GET` | `/healthz` | Component and database health |
| `POST` | `/v1/events` | Idempotent Event ingestion |
| `POST` | `/v1/extract/dry-run` | Non-persistent Claim extraction |
| `POST` | `/v1/consolidate` | Scoped conflict-consolidation job |
| `POST` | `/v1/recall` | Hybrid, evidence-aware recall |
| `POST` | `/v1/memories` | Explicit pinned-memory ingestion |
| `DELETE` | `/v1/memories/{memory_id}` | Cascading explicit forgetting |
| `POST` | `/v1/episodes` | Episode creation |
| `GET` | `/v1/episodes` | Episode listing |
| `GET` | `/v1/episodes/{episode_id}` | Episode detail |
| `PATCH` | `/v1/episodes/{episode_id}` | Episode transition and reward |
| `POST` | `/v1/episodes/{episode_id}/traces` | Trace append |
| `POST` | `/v1/feedback` | Retrieval feedback and correction |
| `GET` | `/v1/policies` | Induced Policy listing |
| `GET` | `/v1/jobs` | Durable job state |
| `GET` | `/v1/stats` | Storage and token counters |

Request fields, response behavior, and examples live in [api.md](api.md). FastAPI also publishes generated OpenAPI
documentation at `/docs` while the service is running.

## 9. Storage and Operations

The database layer owns WAL mode, busy timeout, connection lifecycle, online backup, and ordered immutable migrations.
Workers use durable jobs with leases, heartbeat, stage, and processed/total progress. Audit logs record state changes and
automatic decisions; LLM spans record operation, provider, model, status, token counts, and latency. `/healthz`, `/v1/stats`,
offline evaluation, and the LongMemEval adapter provide operational and quality visibility.

All runtime paths, provider models, credentials, timeouts, and feature modes come from settings/environment variables. Image
file inputs remain disabled unless explicitly enabled and constrained to configured allow-roots. PostgreSQL is only an
experimental connectivity probe and does not implement HL-Mem storage semantics.

### Development and deployment commands

The combined launcher loads `.env` and starts both the API and Worker:

```bash
uv run python start_server.py
```

For split-process operation, start them separately:

```bash
uv run uvicorn hl_mem.api.server:app --host 127.0.0.1 --port 8200
uv run python -m hl_mem.workers.worker run
```

On Windows, `start_production.bat` enables production mode and requires real embedding and reranking providers. Install the
Hermes adapter with `uv run python install_to_hermes.py --hermes-home <HERMES_HOME>`, then restart Hermes.

The offline suite uses fake providers; the real-provider script requires configured credentials:

```bash
.venv/Scripts/python.exe -m pytest tests/ -q
.venv/Scripts/python.exe tests/e2e_real.py
```

These commands are documented for operators; documentation-only changes should still follow the validation scope stated by
their task. This restructure intentionally does not execute pytest.

## 10. Evolution Constraints

- SQLite-first remains the supported deployment principle until measured capacity or collaboration requirements justify a
  second storage implementation behind existing protocols.
- Experimental capabilities advance to beta/stable only through evaluation and observed audit evidence.
- New backends must match SQLite semantics for transactions, migrations, backup, temporal visibility, and degradation.
- Architecture decisions are appended as ADRs; accepted historical ADRs are not rewritten in place.
