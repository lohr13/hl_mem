# HL-Mem REST API

HL-Mem exposes a FastAPI application with 16 routes. From a working directory containing the required `hl_mem.toml`,
start the service with `uv run python start_server.py`; the default address is `http://127.0.0.1:8200`. Interactive
OpenAPI documentation is available at `/docs` while the service is running.

## Conventions

- Request and response bodies use JSON unless noted otherwise.
- `POST /v1/events` accepts `Idempotency-Key`; the body-level `idempotency_key` is used when the header is absent.
- Validation failures return `422`, missing resources return `404`, and invalid state transitions return `409`.
- Timeouts, models, database paths, and feature modes come from `hl_mem.toml`; provider credentials come only from the
  optional `.env` or same-named process environment variables. See the [configuration reference](configuration.md).
- Recall is scoped to a `namespace` and can filter both valid time (`as_of`) and recorded time (`known_as_of`).

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/healthz` | Check database/component health, version, settings snapshot, and recent LLM/vector metrics |
| `POST` | `/v1/events` | Idempotently ingest an event and enqueue its extraction job |
| `POST` | `/v1/extract/dry-run` | Extract candidate claims and token usage without persisting memory data |
| `POST` | `/v1/consolidate` | Enqueue conflict consolidation for an explicit namespace/slot/tag scope |
| `POST` | `/v1/recall` | Retrieve evidence-aware memory through hybrid search and optional reranking |
| `POST` | `/v1/memories` | Save an explicit pinned memory through the normal ingestion path |
| `DELETE` | `/v1/memories/{memory_id}` | Explicitly forget a memory and propagate withdrawal/staleness |
| `POST` | `/v1/episodes` | Create an experience Episode |
| `GET` | `/v1/episodes` | List Episodes, optionally filtered by status |
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

Important fields include tenant/user/project/agent/session identifiers, `event_type`, `actor_type`, `content`,
`occurred_at`, `source_uri`, and `sensitivity`. Ingestion returns the event identifier and whether it was newly created.

### Save an explicit memory

```bash
curl -X POST http://127.0.0.1:8200/v1/memories \
  -H "Content-Type: application/json" \
  -d '{"text":"Alice prefers dark mode","subject":"Alice","predicate":"preference"}'
```

Provide either `text` or the compatibility field `content`. `subject`, `predicate`, and `qualifiers` are optional.

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
    "token_budget": 1200
  }'
```

The response contains ranked `results`, derived `observations`, applicable `policies`, `total`, and an optional packed
`context`. Set `debug` to `true` to include `search_trace`. Each Claim result can include scores, ranking features,
temporal fields, canonical slot/tags, relations, conflicts, and evidence.

## Experience Requests

Create an Episode with `goal`, optional `session_id`, and optional `task_type`. Append Traces with `action`, optional
`observation`, `error_signature`, and numeric `value`. Complete or otherwise update the Episode through `PATCH`, supplying
`status`, optional reward in the `[0, 1]` range, and `outcome_summary`.

Retrieval feedback requires `feedback_id` and `helpful`; `task_outcome` is optional. A correction can target a Claim with
an idempotent `retract` or `replace` action. Replacement also requires `corrected_text`.

## Source of Truth

Pydantic request/response contracts are defined in [`src/hl_mem/api/schemas.py`](../src/hl_mem/api/schemas.py), and route
behavior is defined in [`src/hl_mem/api/server.py`](../src/hl_mem/api/server.py). The generated OpenAPI schema is the most
precise machine-readable reference.
