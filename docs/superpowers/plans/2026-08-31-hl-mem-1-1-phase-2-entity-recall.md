# HL-Mem 1.1 Phase 2 Exact-Entity Recall Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make high-confidence entity queries retrieve within the correct entity scope before FTS/Dense candidate limits, with zero new LLM calls, no extra normal-query embedding call, and deterministic wide fallback for every uncertain case.

**Architecture:** Extend the existing deterministic alias resolver into an explicit query plan, pass its optional entity scope through the existing FTS and Dense repository calls, and preserve the same two channels and ranking pipeline. FTS adds an indexed entity predicate before `LIMIT`; Dense uses a bounded local scan of linked visible Claims only when entity scope is active. Observe/off/wide paths retain 1.0 behavior.

**Tech Stack:** Python 3.12-3.14, SQLite FTS5, existing entity tables/indexes, local cosine scan, dataclasses, pytest, frozen release comparator.

## Global Constraints

- Base is the merged Phase 1 commit on `develop/1.1`; record the exact SHA in the task branch before editing.
- Reuse `canonical_entities`, `entity_aliases`, `claim_entity_links`, canonical subject/target columns, and `idx_claim_entity_links_entity`. Add no table, migration, Graph store, or public vector-backend method.
- A scope is enforceable only when every non-overlapping active mention resolves uniquely to the same typed canonical entity and `_link_coverage_complete()` succeeds.
- Historical aliases, cross-type same names, overlapping ambiguity, multiple canonical entities, incomplete links, no mention, and storage errors stay wide.
- High confidence makes exactly one `search_query` embedding instead of the original query embedding. It never embeds both. Dense-off makes none.
- Query Expansion remains separately governed and default-off. Entity planning failure must not enable it or call any LLM.
- FTS and Dense remain the only RRF channels. Do not add an entity candidate channel, entity score, hardcoded provider/model behavior, or reranker call.
- Keep namespace, status, valid time, recorded time, intent, and `known_as_of` filtering identical to wide retrieval.
- Explicit user values `off` and `observe` remain unchanged. Only the schema default changes to `enforce`, and this occurs after observe/regression evidence is recorded.
- Trace stores normalized mention metadata, stable IDs/counts/reasons, and timings; it never stores raw query text or residual text.
- The 24-case fixture is deterministic and synthetic. It is a targeted regression set, not a statistical Recall claim.
- Every storage/fallback/default change is a separate commit. Refactor Recall only in Phase 3 after this behavior is frozen.

---

## Task 1: Freeze the 1.0 Entity Baseline and the 24-Case Protocol

**Files:**

- Create: `benchmarks/release/entity_v1_protocol.json`
- Create: `benchmarks/release/entity_v1.py`
- Create: `benchmarks/release/results/entity-v1-baseline.json`
- Create: `tests/unit/test_entity_v1_protocol.py`
- Modify: `docs/benchmark/core-v1.md`

**Interfaces:**

- Protocol ID is `hl-mem-entity-v1`; schema version is `1`; fixture hash covers canonical JSON excluding result fields.
- Exactly 24 cases cover: unique active alias, Chinese/English alias, alias inside longer text, historical alias, cross-type same name, same-span ambiguity, overlapping alias, multiple entities, no entity, incomplete links, empty residual, temporal current/historical, namespace isolation, and one controlled storage-failure fallback.
- Each case declares `expected_scope`, `expected_claim_ids`, `forbidden_entity_ids`, and whether output must equal a captured 1.0 wide result.
- `run_entity_protocol(settings, *, output, mode) -> dict[str, object]` uses synthetic SQLite data and the configured deterministic test embedder/reranker. It records Top 1/5, scope/fallback reason, channel counts, call counts, and P50/P95.
- Baseline result is generated from the unmodified Phase 1 behavior with explicit `entity_constraint_mode="observe"`; it is not edited by hand.

- [ ] **Step 1: Write the protocol validator before the runner**

```python
def test_entity_protocol_has_exactly_24_unique_cases() -> None:
    protocol = load_protocol(PROTOCOL_PATH)
    assert len(protocol["cases"]) == 24
    assert len({case["id"] for case in protocol["cases"]}) == 24
    assert protocol_hash(protocol) == protocol["fixture_sha256"]
```

Also require every approved category, stable entity/Claim IDs, no real names/content, and explicit wide-equivalence declarations for uncertain cases.

- [ ] **Step 2: Run the validator and observe the missing protocol/runner**

```powershell
uv run --frozen python -m pytest tests/unit/test_entity_v1_protocol.py -q --tb=short
```

- [ ] **Step 3: Implement the deterministic fixture loader and runner**

Reuse public ingestion/repository helpers where they do not call models; seed vectors explicitly for deterministic cases. Count calls with recording Embedder/Reranker objects. Do not create a second implementation of entity resolution.

- [ ] **Step 4: Capture and validate the Phase 1 baseline**

```powershell
uv run --frozen python benchmarks/release/entity_v1.py --mode observe --output benchmarks/release/results/entity-v1-baseline.json
uv run --frozen python -m pytest tests/unit/test_entity_v1_protocol.py -q --tb=short
```

Expected: the artifact truthfully records current misses; the test validates its commit, config, fixture hash, call counts, and schema rather than requiring future target metrics.

- [ ] **Step 5: Commit the frozen protocol and baseline**

```powershell
git add benchmarks/release/entity_v1.py benchmarks/release/entity_v1_protocol.json benchmarks/release/results/entity-v1-baseline.json tests/unit/test_entity_v1_protocol.py docs/benchmark/core-v1.md
git commit -m "test: freeze exact-entity recall cases"
```

---

## Task 2: Produce Safe Mention Spans and an Explicit Query Plan

**Files:**

- Modify: `src/hl_mem/recall/entity_query.py`
- Modify: `src/hl_mem/recall/trace.py`
- Modify: `tests/unit/test_batch4_entity_query.py`
- Create: `tests/unit/test_entity_query_plan.py`

**Interfaces:**

```python
EntityScopeMode = Literal["entity", "observe", "wide", "off"]

@dataclass(frozen=True, slots=True)
class QueryEntityMention:
    start: int
    end: int
    alias: str
    entity_id: str
    entity_type: str
    proof_id: str

@dataclass(frozen=True, slots=True)
class QueryEntityPlan:
    resolution: QueryEntityResolution
    entity_id: str | None
    residual_query: str
    search_query: str
    scope_mode: EntityScopeMode
    fallback_reason: str | None
```

- `QueryEntityResolution.mention_spans` contains non-overlapping spans over the NFKC/casefold normalized query. It exposes no original query.
- `residual_query` removes selected spans, collapses Unicode whitespace/punctuation gaps deterministically, and retains property/action/time terms.
- `search_query` is residual when non-empty, otherwise the original query. Low/ambiguous/wide/off always use the original query.
- Retain read-only compatibility properties `rewrite` and `filter_mode` only for existing internal tests/patch points; they delegate to the new fields and are not separate state.
- Fallback reasons are a closed set: `no_mention`, `ambiguous_alias`, `multiple_entities`, `incomplete_links`, `resolution_error`, and `mode_off`.

- [ ] **Step 1: Write failing plan and privacy tests**

```python
def test_high_confidence_plan_removes_only_entity_mention() -> None:
    plan = plan_query_entity(connection, "Pony 在 8 月的部署状态", "default", "enforce")
    assert plan.entity_id == "agent:pony"
    assert plan.residual_query == "在 8 月的部署状态"
    assert plan.search_query == plan.residual_query
    assert plan.scope_mode == "entity"


def test_ambiguous_plan_stays_on_original_query() -> None:
    plan = plan_query_entity(ambiguous_connection, "Pony status", "default", "enforce")
    assert plan.entity_id is None
    assert plan.search_query == "Pony status"
    assert plan.scope_mode == "wide"
```

Cover ASCII boundaries, Chinese mentions, NFKC expansion, overlap, multiple mentions for the same entity, multiple entities, empty residual, historical alias, incomplete links, SQLite failure, and `record()` absence of raw/residual query.

- [ ] **Step 2: Run tests and observe missing plan fields/behavior**

```powershell
uv run --frozen python -m pytest tests/unit/test_entity_query_plan.py tests/unit/test_batch4_entity_query.py -q --tb=short
```

- [ ] **Step 3: Implement deterministic normalization, selection, and residual construction**

Keep one resolver query bounded to 1,024 aliases. Use a normalization helper that builds the normalized text and spans together; never apply normalized offsets to the original string.

- [ ] **Step 4: Run entity and trace regressions**

```powershell
uv run --frozen python -m pytest tests/unit/test_entity_query_plan.py tests/unit/test_batch4_entity_query.py tests/unit/test_entity_resolution.py tests/unit/test_entity_coordinates.py tests/unit/test_search_trace.py -q --tb=short
```

- [ ] **Step 5: Commit query planning semantics**

```powershell
git add src/hl_mem/recall/entity_query.py src/hl_mem/recall/trace.py tests/unit/test_batch4_entity_query.py tests/unit/test_entity_query_plan.py
git commit -m "feat: plan exact-entity search queries"
```

---

## Task 3: Push Entity Scope into FTS and Dense Reads

**Files:**

- Modify: `src/hl_mem/storage/claims.py`
- Modify: `src/hl_mem/recall/candidate_channels.py`
- Modify: `src/hl_mem/recall/staged_pipeline.py`
- Create: `tests/unit/test_entity_scoped_repository.py`
- Modify: `tests/unit/test_batch4_entity_query.py`
- Modify: `tests/unit/test_vector_backend_protocol.py`
- Modify: `tests/unit/test_tokenized_fts_repository.py`

**Interfaces:**

```python
def search_claims_fts(..., namespace: str = "default", *, entity_id: str | None = None) -> list[ClaimRow]: ...
def search_claims_vector(..., namespace: str = "default", *, entity_id: str | None = None) -> list[dict[str, Any]]: ...
def _search_claims_vector_scan(..., namespace: str = "default", *, entity_id: str | None = None) -> list[dict[str, Any]]: ...
```

- FTS appends an `EXISTS` predicate covering `claim_entity_links` or matching canonical subject/target before `ORDER BY ... LIMIT`; it must not join duplicate rows into ranking.
- Dense scoped scan selects only embedded, status/time/namespace-visible Claims whose direct canonical columns or active link match `entity_id`, then computes cosine and limits.
- When `entity_id` is set, `search_claims_vector` deliberately uses the scoped local scan even if sqlite-vec is configured; when absent, existing backend delegation is byte-for-byte unchanged.
- `ChannelRequest.entity_scope_id` replaces post-limit enforce filtering. Observe mode still runs the existing shadow filter after wide channel reads; off/wide do neither.
- `CollectedChannels` reports `entity_scope_applied`, `entity_scope_counts`, and `fallback_reason`; it introduces no channel or score.

- [ ] **Step 1: Add failing pre-limit FTS and Dense tests**

```python
def test_entity_scope_finds_claim_beyond_wide_candidate_limit(repo: ClaimRepository) -> None:
    _seed_many_higher_scoring_other_entities(repo.connection, count=20)
    expected = _seed_target_entity_claim(repo.connection)
    assert [row["id"] for row in repo.search_claims_fts("deployment", 5, entity_id=TARGET)] == [expected]
    assert [row["id"] for row in repo.search_claims_vector(VECTOR, 5, entity_id=TARGET)] == [expected]
```

Also cover link-only, subject-only, target-only, duplicate links, namespace, valid/recorded time, status, limit zero, entity without vectors, and sqlite-vec wide delegation versus scoped local scan.

- [ ] **Step 2: Run focused repository tests and observe unsupported keyword/failing target**

```powershell
uv run --frozen python -m pytest tests/unit/test_entity_scoped_repository.py tests/unit/test_vector_backend_protocol.py tests/unit/test_tokenized_fts_repository.py -q --tb=short
```

- [ ] **Step 3: Implement indexed FTS scope and batched scoped vector scan**

Parameterize all values. Preserve the existing `claim_is_visible()` final check. Keep batches bounded by `vector_batch_size` and retain deterministic `(-score, id)` ordering.

- [ ] **Step 4: Replace enforce post-filtering without removing observe instrumentation**

For `scope_mode="entity"`, pass `entity_id` into both repository calls and do not call `apply_entity_constraint`. For observe, call wide methods then the existing shadow filter only for trace. For storage exceptions during scoped reads, retry once wide and return `fallback_reason="storage_error"`; if the wide read also fails, re-raise the original database error.

- [ ] **Step 5: Run retrieval-channel regressions and commit**

```powershell
uv run --frozen python -m pytest tests/unit/test_entity_scoped_repository.py tests/unit/test_batch4_entity_query.py tests/unit/test_hybrid_priors.py tests/unit/test_reranker.py tests/unit/test_query_expansion.py tests/unit/test_search_trace.py tests/unit/test_vector_backend_protocol.py -q --tb=short
git add src/hl_mem/storage/claims.py src/hl_mem/recall/candidate_channels.py src/hl_mem/recall/staged_pipeline.py tests/unit/test_entity_scoped_repository.py tests/unit/test_batch4_entity_query.py tests/unit/test_vector_backend_protocol.py tests/unit/test_tokenized_fts_repository.py
git commit -m "feat: scope entity candidates before channel limits"
```

---

## Task 4: Integrate the Search Query and Freeze Failure Fallback

**Files:**

- Modify: `src/hl_mem/application/recall.py`
- Modify: `src/hl_mem/recall/trace.py`
- Modify: `src/hl_mem/recall/candidate_channels.py`
- Create: `tests/unit/test_entity_recall_integration.py`
- Modify: `tests/unit/test_query_expansion.py`
- Modify: `tests/unit/test_recall_characterization_v0293.py`
- Modify: `tests/unit/test_search_trace.py`

**Interfaces:**

- `_QueryExpansionSession` plans the entity before the first embedding, sets the original `WeightedQuery` text to `plan.search_query` only for `scope_mode="entity"`, and creates one blob for that text.
- `RecallConfig` receives `entity_scope_mode` and `entity_scope_id`; the old field names remain thin aliases only where current internal patch tests require them.
- When entity resolution or scoped channel collection fails, the same recall retries the original wide query with the already available original query embedding only if no scoped embedding was made. If a scoped embedding was already made, it embeds the original once only on the exceptional fallback and records `fallback_embedding_calls=1`; normal successful queries still use one call.
- Trace adds `entity_residual_term_count`, `entity_scope_counts`, `entity_scope_us`, `entity_fallback_reason`, and `entity_fallback_embedding_calls`. No text fields are added.

- [ ] **Step 1: Write failing call-count, fallback, and end-to-end tests**

```python
def test_high_confidence_recall_embeds_residual_once() -> None:
    embedder = RecordingEmbedder()
    result = _service(embedder).recall(query="Pony deployment status", limit=5)
    assert embedder.texts == ["deployment status"]
    assert result["claims"][0]["id"] == "pony-deployment"


def test_ambiguous_recall_is_identical_to_wide_baseline() -> None:
    assert _recall(ambiguous_db, mode="enforce") == _recall(ambiguous_db, mode="off")
```

Cover dense disabled, empty residual, query expansion disabled, scoped SQLite failure, wide fallback failure, cross-entity Top 1, raw-query absence in trace, and ordinary-query call counts.

- [ ] **Step 2: Run integration tests and observe the original-query/post-limit behavior**

```powershell
uv run --frozen python -m pytest tests/unit/test_entity_recall_integration.py tests/unit/test_query_expansion.py tests/unit/test_recall_characterization_v0293.py -q --tb=short
```

- [ ] **Step 3: Wire the plan through Recall without adding orchestration abstractions**

Keep `_QueryExpansionSession` in `application/recall.py` until Phase 3. Pass simple immutable fields through existing `RecallConfig`/`ChannelRequest`; do not introduce a container or generic retrieval filter protocol.

- [ ] **Step 4: Verify Trace privacy and all fallback paths**

```powershell
uv run --frozen python -m pytest tests/unit/test_entity_recall_integration.py tests/unit/test_search_trace.py tests/unit/test_recall_score_output.py tests/unit/test_query_expansion.py -q --tb=short
```

- [ ] **Step 5: Commit integration behavior**

```powershell
git add src/hl_mem/application/recall.py src/hl_mem/recall/trace.py src/hl_mem/recall/candidate_channels.py tests/unit/test_entity_recall_integration.py tests/unit/test_query_expansion.py tests/unit/test_recall_characterization_v0293.py tests/unit/test_search_trace.py
git commit -m "feat: execute exact-entity recall plans"
```

---

## Task 5: Change the Beta Default from Observe to Enforce

**Files:**

- Modify: `src/hl_mem/config/models.py`
- Modify: `tests/unit/test_config_loader.py`
- Modify: `tests/unit/test_settings_contract.py`
- Modify: `docs/config-schema.json`
- Modify: `docs/configuration.md`
- Modify: `docs/capability-matrix.md`
- Modify: `docs/CHANGELOG.md`

**Interfaces:**

- `RetrievalConfig.entity_constraint_mode` default becomes `"enforce"`.
- Loading an explicit schema-v1 `off` or `observe` value preserves it exactly; config migration does not rewrite explicit values.
- The generated schema/default docs and capability matrix identify this as a Beta default change with wide fallback.

- [ ] **Step 1: Write the failing default/preservation tests**

```python
def test_entity_constraint_defaults_to_enforce_but_preserves_explicit_observe() -> None:
    assert Settings.for_test().entity_constraint_mode == "enforce"
    assert load_settings(_config_with("recall.entity_constraint_mode", "observe")).entity_constraint_mode == "observe"
```

Also assert explicit off, snapshot output, config-schema default, and no migration rewrite.

- [ ] **Step 2: Run config tests and observe the current `observe` default**

```powershell
uv run --frozen python -m pytest tests/unit/test_config_loader.py tests/unit/test_settings_contract.py tests/unit/test_config_migrate.py -q --tb=short
```

- [ ] **Step 3: Change one default and regenerate the schema**

```powershell
uv run --frozen python scripts/check_config_schema_snapshot.py --write
uv run --frozen python scripts/generate_configuration_reference.py
```

Review both diffs; only approved entity-default text/schema changes are allowed.

- [ ] **Step 4: Run config/contract checks and commit**

```powershell
uv run --frozen python -m pytest tests/unit/test_config_loader.py tests/unit/test_settings_contract.py tests/unit/test_config_migrate.py -q --tb=short
uv run --frozen python scripts/check_config_schema_snapshot.py
git add src/hl_mem/config/models.py tests/unit/test_config_loader.py tests/unit/test_settings_contract.py docs/config-schema.json docs/configuration.md docs/capability-matrix.md docs/CHANGELOG.md
git commit -m "feat: enforce high-confidence entity scope by default"
```

---

## Task 6: Run the Frozen Entity and Core 1.0 Gates

**Files:**

- Create: `benchmarks/release/results/entity-v1-1.1.0.json`
- Create: `benchmarks/release/results/core-v1-1.1.0.json`
- Modify: `docs/benchmark/core-v1.md`
- Modify: `docs/CHANGELOG.md`

**Interfaces:**

- Entity result records per-case expected/actual IDs, scope/fallback, call counts, and timings without query/Claim content.
- Comparator enforces: high-confidence expected Claims all Top 5; cross-entity Top 1 count 0; declared wide cases equal baseline; new LLM calls 0; normal embedding calls unchanged; P95 `<= max(baseline + 10ms, baseline * 1.10)`.
- Core comparator enforces each approved Recall/abstention metric regression `<=0.01`, HTTP success 100%, and forbidden-status hits 0.

- [ ] **Step 1: Add failing target assertions to the protocol test**

Run against the captured baseline and confirm the target assertion fails on at least the known pre-limit cases. This proves the gate distinguishes the new behavior.

- [ ] **Step 2: Generate the 1.1 entity and Core results**

```powershell
uv run --frozen python benchmarks/release/entity_v1.py --mode enforce --output benchmarks/release/results/entity-v1-1.1.0.json
$commit = git rev-parse HEAD
uv run --frozen python benchmarks/release/core_v1.py --label 1.1.0-dev --commit $commit --output benchmarks/release/results/core-v1-1.1.0.json
uv run --frozen python benchmarks/release/compare_core_v1.py benchmarks/release/results/v1.0.0rc1.json benchmarks/release/results/core-v1-1.1.0.json
```

- [ ] **Step 3: Run the Phase 2 quality gate**

```powershell
uv run --frozen python -m pytest tests/unit/test_entity_v1_protocol.py tests/unit/test_entity_query_plan.py tests/unit/test_entity_scoped_repository.py tests/unit/test_entity_recall_integration.py -q --tb=short
uv run --frozen python -m pytest tests/unit/ -q --tb=short
uv run --frozen python -m ruff check .
uv run --frozen python -m black --check .
uv run --frozen python -m isort --check-only .
uv run --frozen python -m mypy src/hl_mem/ --ignore-missing-imports
uv run --frozen python scripts/check_imports.py
uv run --frozen python scripts/check_complexity_budget.py --ratchet
uv run --frozen python scripts/check_config_schema_snapshot.py
```

- [ ] **Step 4: Inspect behavior and performance before committing evidence**

If a target fails, fix the implementation or report the failed gate. Do not edit result JSON or relax the protocol. Do not claim a population-wide Recall percentage from these 24 cases.

- [ ] **Step 5: Commit sanitized Phase 2 evidence**

```powershell
git add benchmarks/release/results/entity-v1-1.1.0.json benchmarks/release/results/core-v1-1.1.0.json tests/unit/test_entity_v1_protocol.py docs/benchmark/core-v1.md docs/CHANGELOG.md
git commit -m "docs: record exact-entity recall evidence"
```
