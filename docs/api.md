# HL-Mem REST API

HL-Mem exposes a FastAPI application with 16 routes. From a working directory containing the required `hl_mem.toml`,
start the service with `uv run python start_server.py`; the default address is `http://127.0.0.1:8200`. Interactive
OpenAPI documentation is available at `/docs` while the service is running.

## Conventions

- Request and response bodies use JSON unless noted otherwise.
- `POST /v1/events` and `POST /v1/memories` accept `Idempotency-Key`; the body-level `idempotency_key` is used when the
  header is absent.
- Validation failures return `422`, missing resources return `404`, and invalid state transitions return `409`.
- Timeouts, models, database paths, and feature modes come from `hl_mem.toml`; provider credentials come only from the
  optional `.env` or same-named process environment variables. See the [configuration reference](configuration.md).
- Recall, Episode create/list, and Policy listing use an explicit `namespace`. It is a relevance/profile soft label inside
  one trusted local single-tenant deployment, not an authentication, authorization, encryption, or side-channel boundary.
- `tenant_id` is a deprecated compatibility alias for `namespace`; clients should send `namespace`, and conflicting values
  are rejected. Neither field provides SaaS multi-tenant isolation, RBAC, billing isolation, or per-tenant keys.
- Recall can filter both valid time (`as_of`) and recorded time (`known_as_of`).
- `/healthz` is an async, database-free process liveness endpoint. It reports version, effective settings, component state,
  and in-memory metrics without opening a SQLite connection or calling an external provider; use database-backed read-only
  audit surfaces when storage or historical LLM span statistics are required.
- Every HTTP request emits `request_started` and `request_finished` INFO records with method, path, status, and duration;
  a caller-supplied `X-Request-ID` is sanitized and included when present.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/healthz` | Check process/component liveness, version, settings snapshot, and in-memory metrics without database I/O |
| `POST` | `/v1/events` | Idempotently ingest an event and enqueue its extraction job |
| `POST` | `/v1/extract/dry-run` | Extract candidate claims and token usage without persisting memory data |
| `POST` | `/v1/consolidate` | Enqueue conflict consolidation for an explicit namespace/slot/tag scope |
| `POST` | `/v1/recall` | Retrieve evidence-aware memory through hybrid search and optional reranking |
| `POST` | `/v1/memories` | Save an explicit pinned memory through the normal ingestion path |
| `DELETE` | `/v1/memories/{memory_id}` | Explicitly forget a memory and propagate withdrawal/staleness |
| `POST` | `/v1/episodes` | Create an experience Episode in a namespace |
| `GET` | `/v1/episodes` | List Episodes by namespace, optionally filtered by status |
| `GET` | `/v1/episodes/{episode_id}` | Return one Episode and its details |
| `PATCH` | `/v1/episodes/{episode_id}` | Update Episode status/outcome and optionally back-propagate reward |
| `POST` | `/v1/episodes/{episode_id}/traces` | Append an action/observation Trace to an Episode |
| `POST` | `/v1/feedback` | Submit retrieval feedback and an optional retract/replace correction |
| `GET` | `/v1/policies` | List induced Policies by status |
| `GET` | `/v1/jobs` | Return job counts and queue contents |
| `GET` | `/v1/stats` | Return event, claim, token, and pending-job counts |

## Core Requests

### Ingest an event

```bash
curl -X POST http://127.0.0.1:8200/v1/events \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: conversation-42-message-7" \
  -d '{
    "event_type": "message",
    "actor_type": "user",
    "session_id": "conversation-42",
    "content": {"text": "Alice prefers dark mode"}
  }'
```

Important fields include namespace/user/project/agent/session identifiers, `event_type`, `actor_type`, `content`,
`occurred_at`, `source_uri`, and `sensitivity`. Ingestion returns the event identifier and whether it was newly created.

### Save an explicit memory

```bash
curl -X POST http://127.0.0.1:8200/v1/memories \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: profile-alice-dark-mode" \
  -d '{
    "namespace":"profile-alice",
    "text":"Alice prefers dark mode",
    "subject":"Alice",
    "predicate":"preference"
  }'
```

Provide either `text` or the compatibility field `content`. `subject`, `predicate`, `qualifiers`, `namespace`, and
`idempotency_key` are optional. The header key takes precedence over the body key. Repeating the same key and canonical
payload returns the original ID with `created=false`; reusing the key for another payload returns `409`. Omitting the key
preserves the existing create-on-each-call behavior.

### Recall memory

```bash
curl -X POST http://127.0.0.1:8200/v1/recall \
  -H "Content-Type: application/json" \
  -H "X-Request-ID: recall-42" \
  -d '{
    "query": "What does Alice prefer?",
    "limit": 5,
    "namespace": "default",
    "context_mode": "packed",
    "token_budget": 1200,
    "response_format": "both"
  }'
```

The response contains ranked `results`, derived `observations`, applicable `policies`, `total`, and an optional packed
`context`. Set `debug` to `true` to include `search_trace`. Each Claim result can include scores, ranking features,
temporal fields, canonical slot/tags, relations, conflicts, and evidence.

`response_format` accepts `legacy`, `context_packet`, or `both` and defaults to `legacy`. `context_packet` returns only
the optional `context_packet` envelope; `both` returns it alongside the legacy fields. Context Packet v1 has exactly
eight top-level fields: `schema_major`, `schema_minor`, `query_id`, `answerability`, `feedback_state`, `items`,
`used_tokens_estimate`, and `truncated`. Each ordered item has exactly `type`, `id`, `text`, `evidence`, and a newly
materialized non-empty `feedback_id`; its array position is its rank. Claim text comes only from the stored
`index_text` projection.

`feedback_state=available` means every item exposure was atomically persisted. `degraded` means the packet text remains
usable, but its feedback identifiers must not be submitted. Exposure persistence failure never turns a successful
recall into an HTTP 503. The internal `injected` flag means an adapter explicitly delivered the item across the Agent
host/model input boundary; it does not claim that the model read, adopted, or cited the memory.
Successful `legacy` responses retain item-level `feedback_id` values for compatibility; if their exposure batch cannot
be confirmed, those identifiers are omitted rather than returning unusable receipts without a `feedback_state`.

## Experience Requests

Create an Episode with `goal`, `namespace` (default `default`), optional `session_id`, and optional `task_type`. Listing
requires the same soft partition to avoid cross-profile aggregation. Append Traces with `action`, optional `observation`,
`error_signature`, and numeric `value`. Complete or otherwise update the Episode through `PATCH`, supplying `status`,
optional reward in the `[0, 1]` range, and `outcome_summary`. Policy induction buckets Episodes by namespace, and every
supporting Episode must share the Policy namespace.

Retrieval feedback requires `feedback_id` and `helpful`; `task_outcome` is optional. A correction can target a Claim with
an idempotent `retract` or `replace` action. Replacement also requires `corrected_text`.

## Source of Truth

Pydantic request/response contracts are defined in [`src/hl_mem/api/schemas.py`](../src/hl_mem/api/schemas.py), and route
behavior is defined in [`src/hl_mem/api/server.py`](../src/hl_mem/api/server.py). The generated OpenAPI schema is the most
precise machine-readable reference.
