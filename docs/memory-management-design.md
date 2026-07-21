# Memory management enhancement

Status: proposed (design only)  
Target migration: `005_memory_management.sql`  
Date: 2026-07-21

## 1. Summary and decisions

This change adds two independent classifications and two independent lifecycle mechanisms to claims:

- `volatility` continues to describe how quickly a fact changes (`ephemeral` or `stable`).
- `scope` describes retention intent (`temporal` or `permanent`).
- TTL expresses when a fact stops being currently true. Only newly written `ephemeral + temporal` claims receive the default seven-day TTL. `ephemeral + permanent` and all `stable` claims receive no automatic TTL.
- Access decay expresses whether an unused memory should lose confidence and eventually leave normal recall. It applies to both volatility classes, uses scope-specific windows, and archives rather than physically deleting claims so evidence links and historical inspection remain intact.

Recall records access only for claims actually returned to the caller. Ranking remains relevance-first: semantic relevance contributes 70% and bounded recency, access, confidence, and importance priors contribute 30%. An optional reranker remains the strongest semantic signal, but no longer erases the non-semantic priors.

The LLM should assign both `scope` and `importance`. Parser defaults and database defaults make old responses, fake extractors, direct repository inserts, and existing databases safe.

## 2. Source findings that shape the design

The following current behavior was verified in the repository:

- `Database._migrate()` applies lexically ordered SQL files with `executescript`, so `005_memory_management.sql` is sufficient and is naturally idempotent at the migration-file level.
- `ClaimRepository.search_claims_fts()` returns BM25-ordered candidates. `list_embedded()` loads every eligible embedding and `hybrid_claims()` computes every cosine in Python. At 100,000 2048-dimensional claims this full scan, not the additional priors, is the dominant latency risk.
- `hybrid_claims()` uses RRF with `k=60`; when enabled, the external reranker wholly determines the final order.
- `/v1/recall` is the point at which final returned claim IDs are known. Observations are appended independently and are outside this proposal's access accounting.
- `store_extracted()` currently computes a five-minute expiry from wall-clock time and does not store `importance`.
- `ExtractedClaim` is constructed positionally in tests and in the explicit-memory path. New dataclass fields therefore must be appended, not inserted.
- `expire_claims()` returns exactly `{"expired": n}` and the worker calls it every 600 seconds. That return contract is tested.
- The current `claims_au` trigger rebuilds an FTS entry after *any* claim update. Incrementing access count or decaying confidence would therefore churn FTS unless the trigger is narrowed.
- Existing repositories select `*`, so added columns flow through without changing read shapes. Existing API responses explicitly select their public fields, so the wire response remains backward-compatible.

## 3. Schema migration

Create `src/hl_mem/storage/migrations/005_memory_management.sql` with exactly the following DDL:

```sql
ALTER TABLE claims ADD COLUMN scope TEXT NOT NULL DEFAULT 'permanent'
    CHECK (scope IN ('temporal', 'permanent'));
ALTER TABLE claims ADD COLUMN access_count INTEGER NOT NULL DEFAULT 0
    CHECK (access_count >= 0);
ALTER TABLE claims ADD COLUMN last_accessed_at TEXT;
ALTER TABLE claims ADD COLUMN last_decayed_at TEXT;

CREATE INDEX IF NOT EXISTS idx_claims_decay
    ON claims(status, scope, last_accessed_at, recorded_from)
    WHERE status IN ('active', 'disputed');

DROP TRIGGER IF EXISTS claims_au;
CREATE TRIGGER claims_au
AFTER UPDATE OF subject_entity_id, predicate, value_json ON claims
BEGIN
  INSERT INTO claims_fts(claims_fts, rowid, search_text)
  VALUES ('delete', old.rowid, coalesce(old.subject_entity_id, '') || ' ' ||
          coalesce(old.predicate, '') || ' ' || coalesce(old.value_json, ''));
  INSERT INTO claims_fts(rowid, search_text)
  VALUES (new.rowid, coalesce(new.subject_entity_id, '') || ' ' ||
          coalesce(new.predicate, '') || ' ' || coalesce(new.value_json, ''));
END;
```

Migration notes:

- Existing claims become `permanent`, which is the conservative retention choice. Their existing `expires_at` values are not rewritten: an already scheduled TTL remains honored.
- Existing rows keep `access_count=0` and `last_accessed_at=NULL`. Decay uses `recorded_from` as the fallback inactivity anchor, but the rollout grace described in section 7 prevents an upgrade from immediately archiving old rows.
- SQLite cannot add a table-level constraint without rebuilding the table; column-level `CHECK` constraints provide the required validation without a risky copy migration.
- The partial decay index is small and supports the maintenance scan. No ranking index is useful for logarithmic transforms computed over the bounded candidate set.
- `claims_ai` and `claims_ad` remain unchanged. Narrowing `claims_au` makes access/confidence/status updates O(1) table updates without an FTS delete/insert pair.

No `updated_at` column is added: access and decay have their own clocks, and changing general claim update semantics would exceed this feature.

## 4. Extraction and write path

### 4.1 Extracted model

Modify `src/hl_mem/ingest/extractors.py` by appending fields to preserve all current positional constructors:

```python
@dataclass(frozen=True)
class ExtractedClaim:
    predicate: str
    value: str
    confidence: float = 0.9
    volatility: str = "stable"
    subject: str = "用户"
    qualifiers: dict[str, Any] | None = None
    reason: str = ""
    scope: str = "permanent"
    importance: float = 0.5
```

`FakeExtractor.extract(...)` keeps its signature. It sets `scope="temporal"` for its ephemeral `service_status` output and otherwise uses `permanent`; importance remains `0.5`. This preserves deterministic offline behavior while giving ephemeral fake claims the new TTL.

### 4.2 LLM output

Modify `src/hl_mem/ingest/llm_extractor.py`:

- Extend `SYSTEM_PROMPT` so every claim includes `scope` and `importance`.
- Define scope independently of volatility:
  - `temporal`: useful for a bounded real-world period, such as a trip next week, current project deadline, or temporary service state.
  - `permanent`: durable preference, identity, convention, configuration, or explicit memory intended for long-term retention.
- Define `importance` as a number in `[0, 1]`: `0.0-0.3` incidental, `0.4-0.6` useful, `0.7-0.9` important preference/commitment/constraint, `1.0` explicit must-remember instruction. The prompt must say not to infer importance merely from emotional wording.
- Keep volatility instructions focused on change rate, not retention.

`LLMExtractor._claim(item)` keeps its signature and adds defensive parsing:

```python
scope = item.get("scope", "permanent")
scope = scope if scope in {"temporal", "permanent"} else "permanent"
try:
    importance = min(1.0, max(0.0, float(item.get("importance", 0.5))))
except (TypeError, ValueError):
    importance = 0.5
```

It passes both values using keyword arguments. Confidence should also be clamped to `[0, 1]` in the same change; invalid numeric confidence currently reaches storage unchecked. Old model responses omit the new fields and retain `permanent/0.5`, so current extractor tests remain valid.

Yes, the LLM should assign importance: rules alone cannot reliably distinguish a consequential deadline from incidental content. It is only a bounded 7.5% ranking prior, so extraction noise cannot dominate relevance. Explicit memories override the model with `scope="permanent"` and `importance=1.0`.

### 4.3 TTL assignment and storage

Modify `src/hl_mem/api/pipeline.py`.

Change the signature only by adding an optional injected clock after existing parameters:

```python
def store_extracted(
    connection: Any,
    extracted: Any,
    event: dict[str, Any],
    now: str,
    embedder: Any,
    authority: str | None = None,
    ttl_days: int = 7,
) -> str:
```

Parse the supplied `now` once (rather than calling `datetime.now()`), and derive fields as follows:

```python
scope = extracted.scope if extracted.scope in {"temporal", "permanent"} else "permanent"
expires_at = (
    datetime.fromisoformat(now) + timedelta(days=ttl_days)
).isoformat() if extracted.volatility == "ephemeral" and scope == "temporal" else None
```

The inserted claim adds:

```python
"scope": scope,
"importance": min(1.0, max(0.0, float(extracted.importance))),
"access_count": 0,
"last_accessed_at": None,
```

`last_decayed_at` may be omitted and use its SQL default `NULL`. The default TTL is configurable through worker config/environment (`memory_temporal_ttl_days` / `HL_MEM_TEMPORAL_TTL_DAYS`) but defaults to seven. The worker passes it to `store_extracted`; direct callers and current tests need no changes.

The explicit-memory construction in `src/hl_mem/workers/worker.py` should switch to keyword arguments and set stable/permanent/1.0. Keyword construction avoids future positional-field mistakes.

The resulting matrix is:

| volatility | scope | default `expires_at` |
|---|---|---|
| ephemeral | temporal | write time + 7 days |
| ephemeral | permanent | `NULL` |
| stable | temporal | `NULL` |
| stable | permanent | `NULL` |

`stable + temporal` is valid: for example, a reliably known plan whose relevance window is bounded. Its eventual removal is governed by access decay, an explicit `valid_to`, or supersession—not TTL.

Exact-duplicate and entailment paths continue returning the existing claim. They add evidence but do not reset access counters or silently change scope/importance; changing metadata during dedup needs an evidence-weighted merge policy and is out of scope.

## 5. Multi-factor recall ranking

### 5.1 Candidate generation

Retain FTS, dense retrieval, RRF, and the optional reranker. Priors only reorder a bounded candidate pool; they must never scan the full table.

Use an over-fetch size independent of reranker state:

```text
candidate_limit = min(200, max(limit * 5, 50))
```

This is important because non-semantic priors cannot promote a claim that was never fetched. Both FTS and vector retrieval request this size. RRF continues to use `k=60`.

### 5.2 Feature definitions

For every union candidate, calculate values in `[0, 1]` at one captured UTC `ranking_now`:

```text
semantic = rrf_score / (2 / 61)
recency = 1 / (1 + age_days / 30)
access_frequency = log1p(access_count) / log1p(max_access_count_in_candidates)
confidence = clamp(claim.confidence, 0, 1)
importance = clamp(claim.importance, 0, 1)
```

`semantic` is clamped to one. `age_days` uses `observed_at`, then `recorded_from`, then zero boost if neither parses. Future timestamps are treated as age zero. If every candidate has zero accesses, access frequency is zero. Log normalization prevents a single heavily accessed claim from monopolizing results.

The pre-rerank score is:

```text
0.70 * semantic
+ 0.08 * recency
+ 0.07 * access_frequency
+ 0.075 * confidence
+ 0.075 * importance
```

Sort descending by score, then by semantic, then `recorded_from` descending, then `id` ascending. Explicit tie-breakers make fake/offline tests deterministic.

These weights intentionally cap all non-semantic signals at 30%. Access is a usefulness hint, not proof of correctness; confidence and importance remain small priors rather than filters.

### 5.3 Reranker interaction

Send the top `candidate_limit` pre-ranked candidates to the reranker. Use each returned relevance score directly after clamping it to `[0, 1]`; do not normalize across the candidate set. The configured `gte-rerank-v2` scores are already bounded relevance scores, and candidate-set normalization would make the same score mean different things for different result counts. Final score is:

```text
0.80 * clamp(reranker_score, 0, 1) + 0.20 * prior_score
```

where:

```text
prior_score = (
    0.08 * recency + 0.07 * access_frequency
    + 0.075 * confidence + 0.075 * importance
) / 0.30
```

This lets the reranker remain decisive while retaining the required factors. If reranking is disabled, skipped, empty, or fails, use the pre-rank order. Preserve current audit outcomes (`disabled`, `skipped`, `applied`, `empty_fallback`, `error_fallback`) and add weights plus per-returned-ID feature/score data to the existing `recall/ranked` audit detail.

Modify `src/hl_mem/api/pipeline.py` signature compatibly:

```python
def hybrid_claims(
    repo: ClaimRepository,
    query: str,
    query_blob: bytes,
    limit: int,
    as_of: str | None,
    reranker: Any = None,
    now: str | None = None,
) -> list[dict[str, Any]]:
```

All existing positional calls remain valid. Put pure scoring helpers in a new `src/hl_mem/recall/ranking.py`:

```python
def memory_features(claim, semantic_score, max_access_count, now) -> dict[str, float]
def memory_score(features, weights=DEFAULT_WEIGHTS) -> float
def blend_reranker_score(reranker_score, features) -> float
```

Keeping these pure makes weight and boundary tests fast and independent of SQLite/network behavior.

### 5.4 Recording access

Add to `ClaimRepository` in `src/hl_mem/storage/repository.py`:

```python
def record_access(self, claim_ids: list[str], accessed_at: str) -> int:
```

It deduplicates IDs and performs one transactionally committed update:

```sql
UPDATE claims
SET access_count = access_count + 1,
    last_accessed_at = ?
WHERE id IN (...)
  AND status IN ('active', 'disputed', 'superseded');
```

Chunk at 500 IDs even though the API currently caps recall at 100, avoiding SQLite variable-limit surprises for future internal callers. Return the total affected rows. A single recall increments each returned claim once, regardless of whether it appeared in both FTS and dense lists.

Modify `src/hl_mem/api/server.py` after `hybrid_claims()` returns successfully and before response assembly:

```python
try:
    ClaimRepository(connection).record_access([claim["id"] for claim in claims], _now())
except Exception as exc:
    try:
        audit_access_record_failure(connection, exc, claim_count=len(claims))
    except Exception:
        pass
```

Only final returned claims count as accessed. Candidates, observations, failed requests, and claims excluded by the limit do not. Historical (`as_of`) recall does count because the caller actually retrieved the memory. `record_access()` is best-effort metadata bookkeeping: wrap it in `try/except`, attempt to append an `access_record_failed` audit event with error type and claim count but no claim text, and continue returning the successful recall. Audit logging is also best-effort here; a second write failure must not mask the recall result. SQLite's configured five-second busy timeout still handles short WAL contention. The update does not alter the response schema.

## 6. Repository and vector-search performance

### 6.1 Required repository changes

Keep `search_claims_fts(query, limit=20, as_of=None)` unchanged.

Replace the unbounded contract with a bounded vector-candidate contract:

```python
def search_claims_vector(
    self,
    query_blob: bytes,
    limit: int = 200,
    as_of: str | None = None,
) -> list[dict[str, Any]]:
```

`hybrid_claims()` calls this method as a thin repository wrapper around the existing `list_embedded(as_of)` plus Python cosine path. Test doubles that expose only `list_embedded(as_of)` remain compatible. Add a code comment at the scan site documenting that the estimated 100,000-claim/2048-dimensional payload is about 819 MB and that indexed vector retrieval must be reconsidered before that scale.

### 6.2 Vector-search scaling follow-up

The current 2048-dimensional Python full scan reads about 819 MB of vector payload for 100,000 claims before Python unpacking and sorting, but current usage is below 100 claims and the near-term estimate is about 2,000. This design therefore keeps the full-scan backend and does not add a SQLite extension, extension-owned virtual tables, or vector-index maintenance.

Start a separate indexed-vector-search design when the deployment approaches 10,000 claims. That design must evaluate measured latency and recall quality along with extension compilation, SQLite/version coupling, virtual-table lifecycle, rebuild behavior, and deployment health checks. `search_claims_vector()` deliberately provides the seam for that later backend without committing this change to one extension.

Performance benchmark (informational, not a merge gate): measure warm p50/p95 vector-search and end-to-end `/v1/recall` latency with reranking disabled at representative sizes, including 2,000 and 10,000 active 2048-dimensional claims. Record hardware, payload size, and Python unpack/cosine time so the indexed-search follow-up has evidence. Measure external reranker latency separately because it is network-bound. Functional tests continue to verify FTS5 and `idx_claims_decay`; they do not assert the presence of an ANN index.

Access recording is one indexed primary-key update of at most 100 rows. Feature scoring is O(candidate_limit), capped at 200. Neither should materially affect the read budget.

## 7. Access-based decay

Add `src/hl_mem/workers/decay.py` with:

```python
def decay_claims(
    connection: sqlite3.Connection,
    now: str | None = None,
    rollout_grace_days: int = 7,
    min_confidence: float = 0.05,
) -> dict[str, int]:
```

Policy:

| scope | begin confidence decay after unused | archive after unused |
|---|---:|---:|
| temporal | 90 days | 180 days |
| permanent | 180 days | 365 days |

The inactivity anchor is `COALESCE(last_accessed_at, recorded_from)`. The task operates on `active` and `disputed` claims only. It runs in one `BEGIN IMMEDIATE` transaction and returns:

```python
{"decayed": decayed_count, "archived": archived_count}
```

Archival is logical deletion:

```sql
UPDATE claims
SET status = 'archived', embedding_dense = NULL, embedding_sparse = NULL
WHERE ... archive threshold ...;
```

This removes claims from current FTS/vector recall through status filtering and releases inline embedding payload while preserving the claim and evidence graph. Physical deletion is deferred to a separately approved garbage-collection policy. Calling this “archived” rather than “deleted” is intentional and consistent with the existing status model.

Confidence decays linearly over the interval between the scope's decay threshold and archival threshold: 90 days for temporal claims and 185 days for permanent claims. For a claim starting at confidence `1.0`, the daily deltas are therefore `(1.0 - min_confidence) / 90` and `(1.0 - min_confidence) / 185`. A claim already below `1.0` reaches the floor earlier; decay never increases confidence.

Decay is elapsed-day-aware and is applied at most once per UTC day. Compute `elapsed_days` from the later of the scope's decay-start timestamp and `last_decayed_at` to the start of the current UTC day, then apply:

```sql
UPDATE claims
SET confidence = MAX(:min_confidence, confidence - :daily_delta * :elapsed_days),
    last_decayed_at = ?
WHERE ... decay threshold ...
  AND (last_decayed_at IS NULL OR last_decayed_at < start_of_current_utc_day);
```

Using elapsed days makes the result predictable even if maintenance misses a day. Archiving runs before decay so a claim is not pointlessly updated twice. A successful access naturally moves the inactivity anchor forward; `last_decayed_at` need not be cleared, because the next decay eligibility check uses both the new inactivity threshold and the per-day guard. Confidence is not restored by access—retrieval does not prove truth. New evidence can later use a separately designed confidence merge policy.

Upgrade safety: for the first `rollout_grace_days` after migration version `005_memory_management` was applied, claims with `last_accessed_at IS NULL` are not decayed or archived. Read `schema_migrations.applied_at` for this guard. This avoids immediately archiving a long-lived pre-upgrade database while preserving truthful `access_count=0` and `last_accessed_at=NULL`. New claims are not exempt because their `recorded_from` is later than migration application.

`expires_at` remains authoritative and separate. A claim already marked `expired`, superseded, retracted, or archived is ignored by decay.

### Worker integration

Modify `src/hl_mem/workers/worker.py`:

- Import `decay_claims`.
- In the existing `current >= next_ttl` block, call `expire_claims`, then `decay_claims`, then audit cleanup. This keeps one 600-second maintenance cadence and avoids another timer.
- Add `_dispatch` support for job type `decay_access` returning `decay_claims(self.connection)`. Keep `expire_ttl` unchanged.
- Optionally read decay windows and minimum confidence from worker config, but the defaults above are the normative policy. Derive each daily delta from its configured decay-to-archive interval rather than configuring a separate multiplicative factor.

Do not change `expire_claims(connection, now=None) -> dict[str, int]` or its SQL predicate. Newly written permanent-scope claims have `expires_at=NULL`; leaving the expiry worker scope-agnostic also honors legacy rows and keeps the existing TTL test green.

## 8. File-by-file change map

| File | Change |
|---|---|
| `src/hl_mem/storage/migrations/005_memory_management.sql` | Add scope/access/decay fields, partial decay index, and narrow the FTS update trigger. |
| `src/hl_mem/ingest/extractors.py` | Append `scope` and `importance`; classify fake ephemeral output as temporal. |
| `src/hl_mem/ingest/llm_extractor.py` | Prompt for independent scope and bounded importance; validate/default both. |
| `src/hl_mem/api/pipeline.py` | Seven-day tiered TTL, persist scope/importance, bounded candidate generation, multi-factor scoring, and reranker blending. |
| `src/hl_mem/recall/ranking.py` | New pure feature/scoring helpers and default weights. |
| `src/hl_mem/storage/repository.py` | Add `record_access` and `search_claims_vector` as a thin wrapper around the current full scan, with a documented scaling threshold. |
| `src/hl_mem/api/server.py` | Best-effort record one access for each final returned claim; log and suppress metadata-write failures. Public request/response models remain unchanged. |
| `src/hl_mem/workers/decay.py` | New scope-aware daily decay and logical archival task. |
| `src/hl_mem/workers/worker.py` | Run decay on the existing 600-second cadence; configure TTL; explicit memory defaults; optional `decay_access` dispatch. |

`src/hl_mem/workers/ttl.py` requires no behavior or signature change.

## 9. Backward compatibility and tests

The existing suite remains green for these reasons:

- Migration defaults satisfy direct claim inserts that know nothing about new columns.
- Existing ephemeral rows with an explicit past `expires_at` still expire; `expire_claims` keeps its exact signature, predicate, and return shape.
- `ExtractedClaim` fields are appended, so all existing positional construction retains its meaning.
- Missing LLM `scope`/`importance` fields default safely; existing JSON fixtures parse unchanged.
- `store_extracted` and `hybrid_claims` only gain trailing optional arguments.
- The test-double fallback to `list_embedded` preserves existing reranker tests.
- For the current equal-default test claims, confidence/importance/access priors tie. Recency is absent in the test double, so existing RRF order remains `first, second`; reranker reversal remains decisive under the 80/20 blend.
- Public recall results and `/v1/stats` are not extended, so exact response assertions remain unchanged.
- Access writes occur after ranking and do not change—or prevent—the response returned by that same request.

Add the following tests:

1. Migration: old database rows receive permanent/zero/null defaults; invalid scope and negative access count fail.
2. FTS trigger: access/confidence updates do not duplicate or remove searchable content; text updates still refresh it.
3. Extraction: missing/invalid fields default, importance clamps, temporal trip example survives seven days, explicit memory is permanent/1.0.
4. TTL matrix: only ephemeral+temporal gets a generated expiry; the expiry is exactly based on injected `now`; legacy explicit expiries still run.
5. Ranking unit tests: each feature can break an otherwise exact semantic tie, semantic relevance still dominates extreme priors, log access is bounded, malformed dates are safe, and ordering is deterministic.
6. Reranking: priors break equal raw reranker scores; score clamping is safe; results are invariant to unrelated candidate-set score ranges; a clearly higher reranker score remains first; error/empty fallback uses multi-factor pre-rank.
7. Access: only final limited results increment, duplicate candidate appearance increments once, zero-result and failed recall do not increment, repeated recalls update the timestamp, and access/audit write failures do not fail recall.
8. Decay: temporal 90/180 and permanent 180/365 boundaries, strict comparison behavior, elapsed-day linear decay, once-per-day guard, minimum confidence floor, missed maintenance days, access resets inactivity, rollout grace, ignored statuses, and evidence survives archival.
9. Worker: scheduled maintenance calls TTL and decay every 600 seconds; `decay_access` dispatch returns the new result without changing `expire_ttl`.
10. Performance: informational full-scan benchmarks at 2,000 and 10,000 claims, with results recorded but no pass/fail latency gate.

Before merge, run the unchanged existing suite first, then the expanded suite. The acceptance condition is zero regressions in the existing 47 tests plus all new tests.

## 10. Rollout and observability

Roll out in this order:

1. Apply migration and verify columns, narrowed trigger, FTS search, and migration timestamp.
2. Deploy write-path defaults and extractor prompt/parser.
3. Deploy bounded candidate handling over the current full-scan vector retrieval and record the baseline benchmark.
4. Enable multi-factor ranking and access recording; monitor SQLite busy errors, recall p50/p95, candidates per channel, and access-update duration.
5. Enable decay initially in report-only mode, emitting counts and oldest inactivity anchors without updates.
6. After the seven-day migration grace and review of report-only counts, enable confidence decay and logical archival.

Extend recall audit detail with feature values and final scores for returned IDs, but do not log claim text. Emit one maintenance summary per decay run containing counts by scope, duration, and configured thresholds. Add stats/observability queries for most accessed claims using `ORDER BY access_count DESC, last_accessed_at DESC`, but do not add them to the existing `/v1/stats` response in this compatibility release.

## 11. Non-goals and follow-ups

- Access count is not user feedback and does not prove usefulness or truth. A future `retrieval_feedback` table should distinguish retrieved, actually used, helpful, and successful-outcome memories.
- Decay does not physically delete claims or evidence.
- Decay does not restore confidence after access.
- This proposal does not change observation ranking/access accounting.
- It does not reinterpret `refresh_after`, `valid_from`, or `valid_to`.
- It does not automatically merge newly extracted scope/importance into an exact duplicate.
- Ranking weights should become configurable only after offline evaluation; per-request arbitrary weights would make behavior hard to audit.

## 12. Acceptance criteria

- Every claim exposes nonnegative `access_count`, nullable `last_accessed_at`, and valid temporal/permanent scope.
- On the normal write path, a successful recall increments exactly the final returned claims once; access or audit metadata-write failure is logged when possible and never fails recall.
- New ephemeral+temporal claims expire after seven days by default; no other new combination receives TTL.
- Recall ordering incorporates all five required signals with semantic relevance dominant and deterministic fallback behavior.
- Scope-specific access decay runs on the existing 600-second worker cadence, no more than once per claim per day, and archives at 180/365 unused days.
- Existing API shapes and function call sites remain compatible and all existing 47 tests pass.
- Full-scan vector performance is benchmarked at 2,000 and 10,000 claims; approaching 10,000 triggers a separate indexed-vector-search design rather than an extension requirement in this change.
