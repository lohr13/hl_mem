# Duplicate/Conflict Two-Layer Governance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Conservatively prevent and fold near-copy Claims, review existing pending dedup candidates without LLM calls, and run the same lightweight maintenance before LongMemEval retrieval.

**Architecture:** A pure domain predicate provides one safety contract for ingest, maintenance, and recall. Ingest merges only safe same-subject near copies; maintenance persists safe pending pairs as non-destructive equivalent edges; recall uses persisted edges plus the same predicate to select one representative and aggregate evidence. Benchmark invokes only deterministic dedup review and deterministic conflict auto-resolution.

**Tech Stack:** Python 3.11+, SQLite, existing ClaimRepository/IngestService/Worker/RecallService, unittest/pytest in GitHub Actions.

## Global Constraints

- Do not change extraction prompts, benchmark datasets, relevance thresholds, or production cosine defaults.
- Do not add an LLM call, schema migration, full-database pair scan, deletion, or supersede operation.
- Keep each behavior in an independent Chinese commit.
- Do not run pytest locally; use direct Python assertions for RED/targeted smoke and GitHub Actions for the full suite.
- Preserve the three unrelated untracked evaluation helper files.

---

### Task 1: Deterministic near-copy contract and ingest source control

**Files:**
- Modify: `src/hl_mem/domain/claims/dedup.py`
- Modify: `tests/unit/test_dedup.py`
- Modify: `tests/unit/test_pipeline.py`

**Interfaces:**
- Produces: `is_safe_near_duplicate(left, right, *, similarity, semantic_threshold, allow_subject_mismatch=False) -> bool`
- Produces: `DETERMINISTIC_NEAR_COPY_REASON = "deterministic_near_copy_v1"`
- Changes: `Deduplicator.find_duplicate()` may return `(existing_id, "near_duplicate")` before falling back to `semantic_candidate`.

- [ ] Add unit tests proving an otherwise-identical near copy is reusable, while different numbers, qualifiers, predicates, disputed state, non-overlapping validity, and same-looking distinct entities are not.
- [ ] Add an ingest test with a constant embedder proving two safe paraphrases create one Claim, two evidence links, and no pending pair.
- [ ] Run a direct Python import/call before implementation and confirm the new symbol/behavior is absent (RED), without invoking pytest.
- [ ] Implement NFKC/casefold lexical normalization, protected atom extraction, and the pure safety predicate with fixed lexical threshold `0.90`.
- [ ] Update `find_duplicate()` to score existing candidates once, prefer the highest safe near copy, and preserve the old best semantic candidate fallback.
- [ ] Run direct Python assertions for safe and protected examples, then ruff/black/isort/mypy checks.
- [ ] Commit as `摄入：在入库前折叠安全近重复声明`.

### Task 2: Bounded non-destructive maintenance review

**Files:**
- Modify: `src/hl_mem/workers/deduplicate.py`
- Modify: `src/hl_mem/workers/worker.py`
- Modify: `tests/unit/test_dedup.py`
- Modify: `tests/unit/test_worker.py`

**Interfaces:**
- Produces: `review_pending_near_duplicates(connection, *, threshold=0.92, limit=200, reviewed_at=None) -> dict[str, int]`
- Consumes: `is_safe_near_duplicate()` and existing `dedup_pairs` rows.

- [ ] Add tests proving safe pending pairs become `equivalent` while both Claims remain active, protected-atom mismatches remain pending, and scan limit is honored.
- [ ] Add a Worker maintenance-routing test proving deterministic review runs when `dedup_enabled` is true even without an LLM key.
- [ ] Verify RED by directly importing the missing function.
- [ ] Implement one bounded pending-row query, one batched Claim load, compare-and-set updates, and result counts `scanned/equivalent/deferred/missing`.
- [ ] Exclude `DETERMINISTIC_NEAR_COPY_REASON` rows from the existing destructive equivalent apply query.
- [ ] Add the review operation beside `auto_resolve_conflicts` in `_run_maintenance`; do not enqueue or call an LLM.
- [ ] Run direct SQLite smoke assertions and static checks.
- [ ] Commit as `维护：非破坏性确认近重复等价组`.

### Task 3: Equivalent-group recall folding and evidence aggregation

**Files:**
- Modify: `src/hl_mem/storage/claims.py`
- Modify: `src/hl_mem/recall/staged_pipeline.py`
- Modify: `src/hl_mem/application/recall.py`
- Modify: `tests/unit/test_recall_fold_temporal_cleanup.py`
- Modify: `tests/unit/test_m4_recall_semantics.py`

**Interfaces:**
- Produces: `ClaimRepository.find_equivalent_claim_pairs(claim_ids) -> list[tuple[str, str]]`
- Changes: `fold_similar_claims(..., equivalent_pairs=())` annotates representatives with `_equivalent_ids`.
- Changes: recall result may contain `equivalent_claim_ids`, and its `evidence` is the deduplicated union for the group.

- [ ] Add tests for persistent edge grouping, dynamic cross-subject safe near-copy grouping, different-number preservation, highest-score representative selection, and evidence union.
- [ ] Verify RED with a direct fold call that currently returns both aquarium-size Claims.
- [ ] Add the repository batch edge query, constrained to confirmed equivalent decisions and requested Claim IDs.
- [ ] Refactor the existing fold to use persisted connected components first and the shared safety predicate second; retain `dedup_candidate_limit` bounding.
- [ ] Record folded members as `equivalent_folded` in search trace and aggregate evidence in `_assemble_results()` without writing the database.
- [ ] Run a direct fold smoke on the cached `eeda8a6d` aquarium pair and static checks.
- [ ] Commit as `召回：按等价组折叠结果并合并证据`.

### Task 4: LongMemEval maintenance alignment

**Files:**
- Modify: `evaluation/tools/run_longmemeval_benchmark.py`
- Modify: `tests/unit/test_longmemeval_batching.py`
- Modify: `tests/unit/test_longmemeval_benchmark_script.py`

**Interfaces:**
- Produces: `_run_case_maintenance(connection, settings) -> dict[str, object]`
- Changes: each regular case result has a `maintenance` field populated before retrieval for fresh and cached DBs.

- [ ] Add tests proving the helper routes only deterministic dedup review and `auto_resolve_conflicts`, and `_run_case` calls it between ingest/cache-open and recall.
- [ ] Verify RED by importing the missing helper.
- [ ] Implement the helper with the active `dedup.threshold` and `dedup.scan_limit`, and add a benchmark maintenance protocol identifier to run metadata.
- [ ] Store maintenance counts in result JSON; do not call `_run_maintenance`, TTL, decay, purge, or any LLM-backed operation.
- [ ] Run static checks for evaluation and contract files.
- [ ] Commit as `评测：在召回前执行轻量生产维护`.

### Task 5: Documentation, bounded replay, and CI

**Files:**
- Modify: `docs/CHANGELOG.md`
- Modify: `docs/architecture.md`
- Modify: `evaluation/README.md`

- [ ] Document source near-copy reuse, non-destructive pending-pair review, recall evidence grouping, and benchmark maintenance scope/known limitation.
- [ ] Run ruff, black, isort, mypy, import-boundary, documentation consistency, OpenAPI snapshot, MCP snapshot, and `git diff --check` without local pytest.
- [ ] If credentials and cached artifacts permit, replay only `eeda8a6d` (and at most one additional failed case) with `--skip-ingest`; report Top-10 changes separately from answer correctness.
- [ ] Commit docs as `文档：说明重复与矛盾双层治理口径`.
- [ ] Push all commits to `origin/main`, monitor GitHub Actions to completion, diagnose and fix any failures in a separate small commit.
