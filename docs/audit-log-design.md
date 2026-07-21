# Audit and observability logging design

Status: design only. This document proposes `004_audit_log.sql`,
`src/hl_mem/observability/audit.py`, and call sites; it does not add them.

## Goals and limits

The audit stream records decisions, inputs sufficient to reconstruct those decisions,
outputs, and phase timings. It is best-effort and must never participate in a main
pipeline transaction. An audit failure may lose audit records, but may not fail,
delay, roll back, or change an event, job, claim, observation, or recall response.

There is an important measurement limit: an audit trail can automatically measure
rates, disagreements, ranking movement, and latency, but cannot determine whether a
fact is true or a result is relevant without ground truth. The analysis script should
therefore produce both automatic diagnostics and a stratified review set. Optional
human labels are stored separately from immutable audit events and are used for the
reported precision/recall/error metrics.

The design treats these as the eight audited phases:

1. `filter`
2. `extraction`
3. `dedup`
4. `conflict`
5. `recall`
6. `ttl`
7. `observation`
8. `job` (end-to-end and dispatch performance)

Event acceptance is additionally recorded as phase `ingest`, and token-budget
decisions as phase `budget`, because they explain missing extractions.

## Schema: `004_audit_log.sql`

Use one append-only fact table and one optional analyst-label table. Do not declare
foreign keys to operational tables: audit history must survive forgetting, expiry,
and future retention/deletion of source rows.

```sql
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    occurred_at TEXT NOT NULL,            -- UTC ISO-8601 generated at emit time
    phase TEXT NOT NULL,                  -- controlled vocabulary above
    action TEXT NOT NULL,                 -- e.g. evaluated, extracted, exact_match
    outcome TEXT NOT NULL,                -- compact grouping dimension
    duration_us INTEGER,                  -- wall duration of the audited operation
    trace_id TEXT NOT NULL,               -- event job or recall request correlation
    tenant_id TEXT NOT NULL DEFAULT 'default',
    event_id TEXT,
    claim_id TEXT,                        -- principal/new claim for this decision
    related_claim_id TEXT,                -- matched/existing/superseded claim
    query_id TEXT,
    job_id TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}'
        CHECK (json_valid(detail_json)),
    CHECK (duration_us IS NULL OR duration_us >= 0)
);

-- Main dashboard: time window, phase/action/outcome, and latency percentiles.
CREATE INDEX IF NOT EXISTS idx_audit_phase_time
    ON audit_log(phase, occurred_at);

-- Trace reconstruction; time is included to make ordering index-only.
CREATE INDEX IF NOT EXISTS idx_audit_trace_time
    ON audit_log(trace_id, occurred_at, id);

-- Drill-down from operational entities. Partial indexes avoid indexing NULLs.
CREATE INDEX IF NOT EXISTS idx_audit_event_time
    ON audit_log(event_id, occurred_at) WHERE event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_audit_claim_time
    ON audit_log(claim_id, occurred_at) WHERE claim_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_audit_related_claim_time
    ON audit_log(related_claim_id, occurred_at)
    WHERE related_claim_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_audit_query_time
    ON audit_log(query_id, occurred_at) WHERE query_id IS NOT NULL;

-- Optional labels produced by review tooling. target_id is normally audit_log.id,
-- but may be an event, claim, observation, or query ID for aggregate judgments.
CREATE TABLE IF NOT EXISTS audit_review (
    id INTEGER PRIMARY KEY,
    target_type TEXT NOT NULL
        CHECK (target_type IN ('audit','event','claim','observation','query')),
    target_id TEXT NOT NULL,
    question TEXT NOT NULL,                -- extraction_correct, missed_fact, etc.
    label TEXT NOT NULL,                   -- yes/no/partial or a relevance grade
    reviewer TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    note TEXT,
    UNIQUE(target_type, target_id, question, reviewer)
);
CREATE INDEX IF NOT EXISTS idx_audit_review_question
    ON audit_review(question, label);
```

`phase`, `action`, and `outcome` remain ordinary text rather than SQL `CHECK` enums so
a newer binary can safely add an audit outcome before a migration is deployed. Their
allowed values should be constants in `audit.py` and tested.

The indexes deliberately do not index `action`, `outcome`, or JSON independently.
At this workload a phase/time range first narrows the scan; extra indexes cost more
space and write amplification than they save. If a later query repeatedly filters a
JSON key, promote that key to a typed column in a later migration instead of adding
many expression indexes.

## Compact-detail policy

`detail_json` is diagnostic metadata, not a copy of the database:

- Store IDs, ranks, scores, counts, thresholds, reasons, model/version names, token
  counts, and short snapshots needed after a mutable row changes.
- Store event text only once, on the `extraction/evaluated` record, as
  `source_text_preview`: normalized and capped at 512 UTF-8 bytes. Also store
  `source_content_hash`, `source_chars`, and context event IDs. Full source content
  remains in `events.content_json`.
- For each extracted claim, store subject and predicate, plus `value_preview` capped
  at 256 bytes, `value_hash`, confidence, volatility, qualifier keys/compact values,
  and extractor reason capped at 256 bytes. Do not store prompts, embeddings, API
  keys, headers, or full LLM responses.
- Recall stores the query preview capped at 256 bytes and query hash. Candidate arrays
  contain only claim ID and numeric rank/score. Cap each candidate channel at the
  actual `candidate_limit` (currently at most 300 because API `limit <= 100`).
- Error values contain exception class and a sanitized message capped at 256 bytes;
  never a traceback or request payload.
- For `sensitivity != 'normal'`, omit previews entirely and retain hashes, lengths,
  IDs, and structural metadata. A configurable deny-list redactor runs before
  enqueue. The writer applies a final serialized-size cap of 16 KiB; oversized
  details are deterministically reduced and marked `"truncated": true`.

Hashes used for correlation should be SHA-256 hex. Hashes are not anonymization;
access to `audit_log` should match access to the main database.

## `AuditLogger` API and failure model

Proposed public API:

```python
class AuditLogger:
    def __init__(
        self,
        db_path: str | Path,
        *,
        enabled: bool = True,
        queue_size: int = 10_000,
        batch_size: int = 100,
        flush_interval_ms: int = 25,
        max_detail_bytes: int = 16_384,
        busy_timeout_ms: int = 50,
    ) -> None: ...

    def emit(
        self,
        phase: str,
        action: str,
        outcome: str,
        *,
        trace_id: str,
        tenant_id: str = "default",
        event_id: str | None = None,
        claim_id: str | None = None,
        related_claim_id: str | None = None,
        query_id: str | None = None,
        job_id: str | None = None,
        duration_us: int | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> bool:
        """Non-throwing, non-blocking enqueue; False means disabled or dropped."""

    @contextmanager
    def span(self, phase: str, action: str, **dimensions: Any):
        """Yield mutable detail; emit success/error with perf_counter_ns timing."""

    def flush(self, timeout_ms: int = 250) -> bool: ...  # best effort
    def close(self, timeout_ms: int = 500) -> bool: ...  # best effort
    def health(self) -> dict[str, int | bool]: ...


class NullAuditLogger(AuditLogger):
    """All methods are no-ops; used by defaults and tests."""
```

`emit()` must catch every exception, snapshot caller-owned details, perform only
bounded normalization plus `queue.put_nowait`, and normally return in less than 1 ms.
It never opens SQLite, serializes large JSON, logs through the application logger, or
waits for capacity on the calling thread. Queue overflow increments an in-memory
`dropped_queue_full` counter and drops the newest record.

A single daemon writer thread owns a **separate SQLite connection** to the same DB
(`check_same_thread=True`, WAL, `synchronous=NORMAL`, `busy_timeout=50`). It drains up
to 100 records, serializes/redacts there, and writes one `executemany` transaction.
On `locked`, malformed-detail, migration-missing, disk-full, or any other error it
rolls back, increments a reason counter, discards that batch, and continues with
bounded exponential backoff (maximum one second). It never touches the operational
connection or retries indefinitely. Small batches and the short busy timeout bound
writer-lock interference with operational commits.

`health()` exposes emitted, written, dropped, serialization failures, DB failures,
queue depth, and last-success epoch in memory. These counters should be added to
`/v1/stats` only in a later implementation; the audit logger must not recursively
audit itself. At process shutdown, FastAPI lifespan and `Worker.run_forever()` call
best-effort `close()`. A crash may lose the final queued batch; this is the intentional
cost of zero interference. `NullAuditLogger` is the default optional argument for
changed function signatures, preserving current tests and callers.

API and worker processes each create their own logger/writer. SQLite serializes their
short batches. Do not share a logger or connection across processes.

## Integration plan and exact calls

The snippets below are proposed diffs anchored to current functions. They illustrate
all signature changes and exact records; they are not changes to the source tree.
Use `time.perf_counter_ns()` for durations and `_us = (end - start) // 1_000`.

### 1. Filter calibration

File: `src/hl_mem/workers/worker.py`, in `Worker.__init__`, create
`self.audit = config.get("audit") or AuditLogger(self.db_path)`. In `_extract`, replace
the discarded reason (`allowed, _`) immediately around `should_extract`:

```diff
- allowed, _ = self.filter.should_extract({**event, "content": content})
+ started = time.perf_counter_ns()
+ allowed, reason = self.filter.should_extract({**event, "content": content})
+ self.audit.emit(
+     "filter", "evaluated", "allow" if allowed else "reject",
+     trace_id=job_trace_id, event_id=event["id"], job_id=current_job_id,
+     tenant_id=event.get("tenant_id", "default"),
+     duration_us=(time.perf_counter_ns() - started) // 1_000,
+     detail={
+         "reason": reason, "event_type": event["event_type"],
+         "actor_type": event["actor_type"], "content_chars": len(event["content_json"]),
+         "content_hash": event.get("content_hash"),
+     },
+ )
```

Pass `job["id"]` and a stable trace (`event_id`) from `_dispatch` into `_extract`;
do not use thread-local context. Rejected events are later stratified by `reason` for
manual `missed_fact` labeling. Allowed events that produce no claims identify loose
filtering (after separating extractor failures and `should_memorize=false`).

Also in `src/hl_mem/api/server.py`, after `insert_event` and `_queue_event`, emit:

```python
audit.emit(
    "ingest", "accepted", "queued" if created else "duplicate",
    trace_id=event_id, event_id=event_id, tenant_id=payload.tenant_id,
    detail={"event_type": payload.event_type, "actor_type": payload.actor_type,
            "content_chars": len(content_json), "content_hash": event["content_hash"],
            "sensitivity": payload.sensitivity},
)
```

The existing early idempotency return needs the same call with `outcome="duplicate"`
and the existing ID. This distinguishes API deduplication from fact deduplication.

### 2. Extraction quality and budget

File: `src/hl_mem/workers/worker.py`, in `_extract`, time only the extractor call
(including its HTTP retries), then emit one parent record even for zero results:

```python
audit.emit(
    "extraction", "evaluated", "claims" if extracted else "no_claims",
    trace_id=event["id"], event_id=event["id"], job_id=job_id,
    tenant_id=event.get("tenant_id", "default"), duration_us=extract_us,
    detail={
        "extractor": type(self.extractor).__name__,
        "model": getattr(self.extractor, "model", None),
        "extractor_version": "llm-v1" if isinstance(self.extractor, LLMExtractor) else "fake-v1",
        "source_text_preview": preview(EventFilter._text(content), sensitivity=event.get("sensitivity")),
        "source_content_hash": event.get("content_hash"),
        "source_chars": len(event["content_json"]),
        "context_event_ids": [item["id"] for item in recent],
        "claim_count": len(extracted),
        "usage_tokens": self.extractor.last_usage_tokens if isinstance(self.extractor, LLMExtractor) else 0,
        "claims": [claim_summary(item) for item in extracted],
    },
)
```

On an exception, emit the same action with `outcome="error"`, elapsed duration,
extractor/model, exception class, and sanitized message, then re-raise. Explicit
memories use `extractor="explicit_memory"`, zero tokens, and no `recent` dependency.

Immediately after the current `estimate` and `can_spend` decision:

```python
audit.emit(
    "budget", "checked", "allow" if can_spend else "reject",
    trace_id=event["id"], event_id=event["id"], job_id=job_id,
    detail={"estimated_tokens": estimate, **self.budget.get_stats()},
)
```

Compute `can_spend` once and reuse it. After `record_usage`, emit
`budget/recorded/success` with actual tokens and the post-write stats. This separates
budget suppression from filter and extraction behavior.

### 3. Exact and semantic dedup effectiveness

File: `src/hl_mem/api/pipeline.py`, add optional `audit` and `trace_id` parameters to
`store_extracted`. Around `find_by_fact_hash`:

```python
audit.emit(
    "dedup", "fact_hash_checked", "match" if exact else "new",
    trace_id=trace_id, event_id=event["id"], claim_id=claim["id"],
    related_claim_id=exact["id"] if exact else None, tenant_id=namespace,
    duration_us=fact_hash_us,
    detail={"fact_hash": claim["fact_hash"], "predicate": claim["predicate"]},
)
```

File: `src/hl_mem/recall/dedup.py`, extend `find_duplicate` to return a diagnostic
object in addition to its current result (or add `find_duplicate_diagnostic` to avoid
breaking callers). It must record candidate count, method, threshold, matched ID,
matched similarity, and `max_similarity` even when there is no match. Do not record
all embeddings or all candidate values. Back in `store_extracted`, emit:

```python
audit.emit(
    "dedup", "semantic_checked", diagnostic["method"],  # exact|semantic|new
    trace_id=trace_id, event_id=event["id"], claim_id=claim["id"],
    related_claim_id=duplicate_id, tenant_id=namespace,
    duration_us=semantic_us,
    detail={"candidate_count": diagnostic["candidate_count"],
            "threshold": diagnostic["threshold"],
            "matched_similarity": diagnostic.get("matched_similarity"),
            "max_similarity": diagnostic.get("max_similarity")},
)
```

Note that this semantic path currently runs **only when no conflict-key candidates
exist**. The report must show that coverage explicitly; it must not interpret absent
semantic checks as semantic non-matches. Human labels `dedup_correct` and
`missed_duplicate` measure over- and under-merging respectively.

### 4. Conflict resolution accuracy

File: `src/hl_mem/api/pipeline.py`, in `store_extracted`, time the current
`ConflictResolver().resolve(existing[-1], ...)` and emit before mutating statuses:

```python
audit.emit(
    "conflict", "resolved", resolution,
    trace_id=trace_id, event_id=event["id"], claim_id=claim["id"],
    related_claim_id=existing[-1]["id"], tenant_id=namespace,
    duration_us=resolve_us,
    detail={
        "conflict_key": claim["conflict_key"], "candidate_count": len(existing),
        "old": claim_summary(existing[-1]), "new": claim_summary(claim),
        "old_status_before": existing[-1]["status"],
        "new_status_after": {"entails": None, "state_change": "active",
                             "contradicts": "disputed", "uncertain": "candidate"}.get(resolution),
        "old_status_after": {"state_change": "superseded",
                             "contradicts": "disputed"}.get(resolution, existing[-1]["status"]),
        "resolver_version": "deterministic-v1",
    },
)
```

`claim_summary` includes value preview/hash, predicate, qualifiers, authority, validity
times, and confidence. Emit `conflict/not_applicable/no_existing` when the lookup is
empty, including lookup duration and conflict key. This makes phase latency complete
and reveals how often the resolver is reached. Label sampled resolutions with
`conflict_correct` and, when wrong, the expected outcome.

### 5. Recall and reranking quality

File: `src/hl_mem/api/server.py`, create `query_id` **before** calling
`hybrid_claims` (using the request header or `new_id()` exactly once) and pass it,
`audit`, and tenant/trace data into `hybrid_claims`. Return that same ID.

File: `src/hl_mem/api/pipeline.py`, extend `hybrid_claims(..., audit=None,
query_id=None, trace_id=None, tenant_id="default")`. Time FTS, dense-list/load plus
similarity sort, fusion, and reranker separately. Emit one record after the final
fallback/selection:

```python
audit.emit(
    "recall", "ranked", rerank_outcome,  # disabled|skipped|applied|empty_fallback|error_fallback
    trace_id=trace_id or query_id, query_id=query_id, tenant_id=tenant_id,
    duration_us=total_us,
    detail={
        "query_preview": preview(query), "query_hash": sha256_text(query),
        "limit": limit, "as_of": as_of, "candidate_limit": candidate_limit,
        "fts": rank_items(fts, score_name="bm25_unavailable"),
        "dense": rank_items(dense, include_similarity=True),
        "rrf": rank_items(ranked_claims, scores=scores),
        "reranker": rank_items(reranked_claims, scores=reranker_scores),
        "returned_ids": [claim["id"] for claim in final],
        "rank_change_count": rank_change_count,
        "top1_changed": rrf_ids[:1] != final_ids[:1],
        "mean_abs_rank_delta": mean_abs_rank_delta,
        "timing_us": {"query_embedding": query_embed_us, "fts": fts_us,
                      "dense_load_score_sort": dense_us, "rrf": rrf_us,
                      "reranker": rerank_us, "response_hydration": hydrate_us},
    },
)
```

Two timings live outside `hybrid_claims`: wrap `embedder.embed_one(payload.query)` in
`server.recall` for `query_embedding`, and wrap evidence/observation response assembly
for `response_hydration`; pass/add them before emitting (or emit child
`recall/phase_timed/success` records sharing `query_id`). The latter is cleaner because
the ranked event can be emitted immediately and child durations can be joined.

Current repository FTS results do not expose BM25 and dense similarity is discarded
after sorting. The design requires retaining dense scores locally. Exposing BM25
would require a repository return-shape change; until then log FTS rank only and mark
the score unavailable. `Reranker.rerank()` deliberately converts all failures to an
empty list, so `hybrid_claims` cannot distinguish an error from a legitimate empty
response. Add a non-breaking `last_outcome` (`success|empty|error`) and sanitized
`last_error_class`, or return a typed result, to distinguish the two audit outcomes.

Relevance labels (`0`, `1`, `2`) attach to `(query_id, claim_id)` via review rows or an
analysis-script import. Metrics are Recall@k/NDCG/MRR for RRF and reranked lists;
ranking-change statistics alone measure behavior, not improvement.

Observations are currently appended unranked to every response. Record a child
`recall/observations_appended/success` with observation IDs/count so claim ranking
metrics do not accidentally treat them as ranked candidates.

### 6. TTL safety

File: `src/hl_mem/workers/ttl.py`, extend
`expire_claims(connection, now=None, audit=None, trace_id=None)`. The current bulk
`UPDATE` loses which claims changed. First select only the rows eligible at the same
`reference`, capturing ID, conflict key, subject, predicate, value preview/hash,
expiry, observed time, evidence-link count, and last evidence event time. Perform the
existing guarded update, then emit one summary plus one record per selected claim:

```python
audit.emit(
    "ttl", "claim_expired", "expired",
    trace_id=trace_id, claim_id=row["id"], tenant_id=row["namespace_key"],
    detail={"expires_at": row["expires_at"], "observed_at": row["observed_at"],
            "age_at_expiry_s": age_s, "conflict_key": row["conflict_key"],
            "predicate": row["predicate"], "value_hash": sha256_text(row["value_json"]),
            "value_preview": preview(row["value_json"]),
            "evidence_count": row["evidence_count"],
            "last_evidence_at": row["last_evidence_at"]},
)
audit.emit(
    "ttl", "sweep", "success", trace_id=trace_id, duration_us=sweep_us,
    detail={"eligible_count": len(rows), "updated_count": cursor.rowcount,
            "reference": reference},
)
```

Emit `ttl/sweep/error` before re-raising operational TTL failures. Audit failures are
still swallowed. Generate a unique sweep trace in both `run_forever` and the
`expire_ttl` dispatch path.

Safety is evaluated two ways: reviewer label `still_useful_at_expiry`, and an offline
counterfactual that compares later recall query text/embeddings to expired claims.
The latter is a “potentially useful” proxy, not proof. Logging the value snapshot is
necessary because a later forget operation nulls embeddings and retention may remove
the operational row.

### 7. Observation building

File: `src/hl_mem/api/pipeline.py`, pass audit context from `store_extracted` into
`_build_observation`. Emit on both branches of `try_build`:

```python
audit.emit(
    "observation", "build_attempted", "built" if built else skip_reason,
    trace_id=trace_id, event_id=event_id, claim_id=new_claim_id,
    tenant_id=tenant_id, duration_us=build_us,
    detail={"conflict_key": conflict_key, "candidate_count": len(claims),
            "active_count": active_count, "distinct_event_count": distinct_event_count,
            "thresholds": {"min_proofs": builder.MIN_PROOFS,
                           "min_sources": builder.MIN_SOURCES},
            "claim_ids": [item["id"] for item in claims],
            "observation_id": observation_id if built else None,
            "body_preview": preview(built["body"]) if built else None,
            "body_hash": sha256_text(built["body"]) if built else None,
            "confidence": built["confidence"] if built else None},
)
```

`ObservationBuilder.try_build` currently returns only `None`, which hides whether it
failed on active proof count, topic agreement, or distinct evidence count. Add a
diagnostic method returning `(built, reason, counts)` while retaining `try_build` as a
compatibility wrapper. Suggested skip outcomes are `insufficient_active_claims`,
`mixed_topic`, and `insufficient_distinct_events`.

In `stale_observations`, emit `observation/marked_stale/claim_forgotten` for each
affected observation with observation and claim IDs. Review labels should cover
`observation_useful`, `observation_faithful`, and `observation_redundant`.

### 8. Job and end-to-end performance

File: `src/hl_mem/workers/worker.py`, in `run_once`, start timing immediately after a
lease succeeds. Emit after completion and after failure handling:

```python
audit.emit(
    "job", "dispatched", final_status,
    trace_id=event_id_if_extract_else_job_id, event_id=event_id_if_extract,
    job_id=job["id"], duration_us=job_us,
    detail={"job_type": job["job_type"], "attempt": job["attempts"],
            "queue_delay_us": iso_delta_us(job["created_at"], lease_started_at),
            "result_claims": result.get("claims"),
            "error_class": type(error).__name__ if error else None,
            "error": safe_error(error)},
)
```

Also emit `job/leased/success` with lease-query duration and queue delay. Because
`run_once` does not currently pass job context into `_extract`, introduce explicit
parameters rather than ambient globals. The phase-specific timings above plus this
end-to-end record locate queueing, filter, extractor/API, embedding, storage,
observation, FTS/dense, reranker, hydration, and TTL costs.

## Analysis script outline

Proposed command:

```text
python scripts/analyze_audit.py --db hl_mem.db --since 2026-07-07 --until 2026-07-21 \
    --sample-dir audit-review --report audit-report.md [--labels labels.csv]
```

It opens SQLite read-only, validates the audit schema and drop counters exported at
shutdown if available, imports optional labels idempotently, emits CSV/JSON review
samples, and produces one Markdown report. SQLite has no built-in percentile, so the
script loads bounded duration columns and computes p50/p90/p95/p99 in Python. All SQL
uses half-open UTC windows: `occurred_at >= :since AND occurred_at < :until`.

### 1. Extraction quality

Automatic yield and error diagnostics:

```sql
SELECT outcome, json_extract(detail_json, '$.extractor') AS extractor,
       COUNT(*) AS events,
       SUM(COALESCE(json_extract(detail_json, '$.claim_count'), 0)) AS claims,
       AVG(COALESCE(json_extract(detail_json, '$.claim_count'), 0)) AS claims_per_event,
       SUM(COALESCE(json_extract(detail_json, '$.usage_tokens'), 0)) AS tokens
FROM audit_log
WHERE phase='extraction' AND action='evaluated'
  AND occurred_at>=:since AND occurred_at<:until
GROUP BY outcome, extractor;
```

Stratify a review sample by extractor, event type, zero/nonzero claims, confidence,
and volatility. Join `audit_review` to calculate claim-level precision
(`extraction_correct`), event-level missed-fact rate (`missed_fact`), hallucination
rate (`unsupported_claim`), and sensitivity leakage. Report label count and 95%
confidence intervals; if no labels exist, say “not measured,” not “accurate.”

### 2. Filter calibration

```sql
SELECT outcome, json_extract(detail_json, '$.reason') AS reason, COUNT(*) AS n
FROM audit_log
WHERE phase='filter' AND action='evaluated'
  AND occurred_at>=:since AND occurred_at<:until
GROUP BY outcome, reason ORDER BY n DESC;
```

Join allowed event IDs to extraction records to compute zero-claim rate and tokens per
stored claim. Review rejected strata for `missed_fact` (false negatives) and allowed
zero-claim/high-cost strata for `should_have_filtered` (false positives). Budget
rejections and extraction errors are excluded from the loose-filter denominator.

### 3. Dedup effectiveness

```sql
SELECT action, outcome, COUNT(*) AS n,
       AVG(json_extract(detail_json, '$.matched_similarity')) AS avg_match_similarity,
       AVG(json_extract(detail_json, '$.candidate_count')) AS avg_candidates
FROM audit_log
WHERE phase='dedup' AND occurred_at>=:since AND occurred_at<:until
GROUP BY action, outcome;
```

Plot semantic `max_similarity` around the threshold, sample both sides, and join
`dedup_correct` labels for over-merge rate. Search new claims within the same
tenant/subject and time window for high pairwise similarity or normalized equal
values, then label `missed_duplicate` for under-merge rate. Report fact-hash and
semantic coverage separately, including the current “semantic not run when a
conflict-key candidate exists” branch.

### 4. Conflict resolution

```sql
SELECT outcome, COUNT(*) AS n,
       AVG(json_extract(detail_json, '$.candidate_count')) AS avg_candidates
FROM audit_log
WHERE phase='conflict' AND action='resolved'
  AND occurred_at>=:since AND occurred_at<:until
GROUP BY outcome;
```

Cross-tab outcome by predicate, authority relation, change qualifier, and subsequent
status. A stratified sample from every outcome receives `conflict_correct` and
`expected_resolution`; the report prints a confusion matrix and accuracy per outcome.

### 5. Recall and reranker

```sql
SELECT outcome, COUNT(*) AS queries,
       SUM(json_extract(detail_json, '$.top1_changed')) AS top1_changed,
       AVG(json_extract(detail_json, '$.rank_change_count')) AS changed_items,
       AVG(json_extract(detail_json, '$.mean_abs_rank_delta')) AS mean_rank_delta
FROM audit_log
WHERE phase='recall' AND action='ranked'
  AND occurred_at>=:since AND occurred_at<:until
GROUP BY outcome;
```

Expand `$.rrf`, `$.reranker`, and `$.returned_ids` with `json_each`. With relevance
labels, compute Precision@k, Recall@k (only where the judged pool is complete), MRR,
and NDCG for both orderings, plus win/tie/loss per query. Without labels, report only
candidate overlap, top-1 change, rank displacement, fallback/error rate, FTS/dense
overlap, and latency; do not call ranking movement an improvement.

### 6. TTL safety

```sql
SELECT date(occurred_at) AS day, COUNT(*) AS expired,
       AVG(json_extract(detail_json, '$.age_at_expiry_s')) AS avg_age_s,
       AVG(json_extract(detail_json, '$.evidence_count')) AS avg_evidence
FROM audit_log
WHERE phase='ttl' AND action='claim_expired'
  AND occurred_at>=:since AND occurred_at<:until
GROUP BY day;
```

Join `still_useful_at_expiry` labels for unsafe-expiry rate. For the proxy, take recall
queries after each expiry and score the stored value preview/full operational value
when available against the query; list high-similarity expired claims and whether a
new claim with the same conflict key appeared. Report this as counterfactual demand.

### 7. Observation usefulness

```sql
SELECT outcome, COUNT(*) AS attempts,
       AVG(json_extract(detail_json, '$.candidate_count')) AS avg_candidates,
       AVG(json_extract(detail_json, '$.confidence')) AS avg_confidence
FROM audit_log
WHERE phase='observation' AND action='build_attempted'
  AND occurred_at>=:since AND occurred_at<:until
GROUP BY outcome;
```

Report build rate, skip-reason distribution, stale rate/time-to-stale, evidence count,
and recall exposure. Human labels produce usefulness, faithfulness, and redundancy
rates. Since observations are currently appended to all recalls, exposure is not
evidence of relevance and must be reported separately.

### 8. Performance

```sql
SELECT phase, action, COUNT(*) AS n, AVG(duration_us) AS mean_us,
       MAX(duration_us) AS max_us
FROM audit_log
WHERE duration_us IS NOT NULL
  AND occurred_at>=:since AND occurred_at<:until
GROUP BY phase, action ORDER BY mean_us DESC;
```

Python adds percentiles and breaks recall child timing out by `timing_us` keys. Report
job queue delay separately from execution, extractor and reranker errors/fallbacks,
attempt counts, audit drop/error counters, and slowest trace IDs for drill-down. Use
`trace_id` to produce a per-event waterfall; phase durations may overlap, so never sum
them blindly when an end-to-end `job` duration exists.

## Estimated storage for 200 events/day for 14 days

There are 2,800 ingested events. A realistic allowed event produces approximately:

- ingest: 1 row
- filter: 1 row
- extraction: 1 row
- budget: 2 rows for an LLM extraction
- dedup/conflict/store path: about 2 rows per extracted claim
- observation attempt: 1 row per newly inserted claim
- job: 2 rows
- TTL: a small number of summary/claim rows

Assuming 70% pass the filter, 1.2 claims per passed event, and normal API recall volume
of about 200 queries/day, this is roughly 24,000 rows over 14 days. With compact
details, use these planning assumptions:

| Component | Assumption | Approximate size |
|---|---:|---:|
| Fixed/text columns + SQLite row overhead | 220 B × 24,000 | 5.3 MiB |
| Average `detail_json` | 650 B × 24,000 | 14.9 MiB |
| Six B-tree indexes | about 45% of table payload | 9.1 MiB |
| Page slack/WAL headroom | about 25% | 7.3 MiB |
| **Expected total** | | **about 37 MiB** |

A conservative range is **25–60 MiB**. Recall candidate arrays dominate the upper
end: 2,800 recalls with 30 candidates/channel add roughly 5–10 MiB; unusually high
`limit` values or 16 KiB-capped details can push beyond the range. With no recalls,
the ingestion-only stream is likely 15–25 MiB. Embeddings are never copied into the
audit DB. Verify after day one with `dbstat` (if enabled), `page_count * page_size`,
row counts, and average `length(detail_json)`, then project to day 14.

Retention should initially be at least 30 days, followed by chunked deletion by
`occurred_at` and `PRAGMA incremental_vacuum` only if the database is configured for
it. Retention work is intentionally outside migration 004 and must itself remain
best-effort/off the request path.

## Trade-offs and rejected alternatives

**Same database versus a separate audit database.** Migration 004 implies the same
SQLite database and gives simple joins to events/claims plus one-file analysis. The
cost is WAL writer contention. A separate DB isolates writes better but complicates
deployment, correlation, backups, and the requested migration. The dedicated
connection, nonblocking queue, short busy timeout, and small batches are the chosen
compromise. Move to a separate DB only if measured lock contention is material.

**Buffered records versus synchronous inserts.** A synchronous insert/commit cannot
reliably stay below 1 ms and can surface `database is locked` or disk errors to the
pipeline. Buffered best-effort writes meet isolation and latency goals at the cost of
losing a tail batch on crashes.

**Drop newest versus blocking or unbounded buffering.** Blocking violates zero
interference; an unbounded queue risks memory exhaustion during an outage. Dropping
newest preserves process health and makes loss measurable through counters.

**One wide table versus a table per phase.** One table supports common time/phase
queries, uniform correlation, simple batching, and additive outcomes. Typed columns
hold frequently grouped dimensions; phase-specific fields stay compact JSON. Tables
per phase provide stronger typing but multiply migrations, writer statements, and
analysis joins.

**Full payloads/prompts versus previews and hashes.** Full payloads improve forensic
replay but duplicate sensitive content and dominate storage. Operational event rows
are the source of truth; bounded previews survive common mutations and make review
sets legible. Exact replay should be a separate, access-controlled feature.

**Sampling versus logging every decision.** Sampling would undermine rare failure,
conflict, TTL, and over-merge analysis at this modest volume. Log every decision, but
sample only the expensive human review.

**OpenTelemetry/log files versus SQLite.** OpenTelemetry is preferable for distributed
tracing and external dashboards, but adds infrastructure and does not alone provide
the durable, locally joinable decision dataset required here. Text logs are difficult
to join and evolve. The proposed schema can later be exported to OTLP without changing
the decision vocabulary.

**Automatic “quality scores” versus labels.** Yield, confidence, similarity, and rank
movement are useful diagnostics but are not truth or relevance. The design rejects a
single proxy quality score and reports labeled metrics with sample size alongside
unlabeled operational signals.

**Instrumenting only public methods.** Logging only `should_extract`, `resolve`, or
`rerank` loses event/job/query correlation and mutation outcomes. Orchestration call
sites own audit emission, while helper methods return diagnostics. This keeps domain
components deterministic and independently testable.

## Implementation acceptance criteria (for the later implementation)

- Every proposed audit call is wrapped by a non-throwing API; tests inject a logger
  that raises internally and verify unchanged pipeline results/statuses.
- A full queue, locked audit writer, missing table, invalid detail, disk-write error,
  and shutdown timeout cannot fail a request or worker job.
- A microbenchmark shows p99 `emit()` caller time below 1 ms with representative
  details; SQLite writing happens only on the writer thread.
- Trace reconstruction covers ingest through job completion and recall through
  hydration, with stable event/job/query IDs.
- Sensitive previews are absent, details never exceed 16 KiB, and no embedding,
  credential, prompt, or full model response is logged.
- The analysis command completes against a copied/read-only two-week database and
  clearly distinguishes automatic diagnostics from label-dependent accuracy.
