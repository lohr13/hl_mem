# Temporal, Subject, and Cache Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete T4 temporal semantics, B1 namespace-safe persona subject canonicalization, and B2 extractor cache isolation without changing service APIs.

**Architecture:** Keep time parsing as a deterministic pure module anchored to event time, keep entity identity as the existing `(namespace_key, subject_entity_id)` pair, and make cache identity explicit through the extractor fingerprint plus per-case MemDaily manifests. Historical subject repair runs as an idempotent Python data migration registered by SQL marker 038.

**Tech Stack:** Python 3.11+, SQLite migrations, FastAPI application model, pytest unit test sources, black, isort, ruff, mypy.

## Global Constraints

- Do not change the public signatures or return structures of `RecallService` or `IngestService` except backward-compatible additions.
- Do not change extraction prompts, index defaults, episodic behavior, retrieval behavior, or judge configuration.
- Do not run pytest, benchmarks, or canaries on this Windows host; CI runs unit tests.
- Locally run only `black --check`, `isort --check-only`, `ruff check`, and `mypy src/hl_mem/` over the changed scope.
- Preserve unrelated untracked workspace files.

---

### Task 1: T4 deterministic temporal intervals

**Files:**
- Modify: `src/hl_mem/ingest/relative_time.py`
- Modify: `tests/unit/test_extraction_language_episodic_time.py`
- Modify: `tests/unit/test_admission_unittest.py`

**Interfaces:**
- Consumes: `infer_occurrence(text: str, occurred_at: str | None) -> tuple[str | None, str | None]`.
- Produces: the same signature, with date/week precision represented as half-open intervals and explicit range recognition.

- [ ] **Step 1: Add table-driven expectations before implementation**

Add cases equivalent to:

```python
(
    "last week",
    "2026-08-08T18:30:00+08:00",
    ("2026-07-27T00:00:00+08:00", "2026-08-03T00:00:00+08:00"),
)
(
    "from last week to yesterday",
    "2026-08-08T18:30:00+08:00",
    ("2026-07-27T00:00:00+08:00", "2026-08-08T00:00:00+08:00"),
)
(
    "May 20, 2023",
    "2026-08-08T18:30:00-05:00",
    ("2023-05-20T00:00:00-05:00", "2023-05-21T00:00:00-05:00"),
)
```

Also cover `this week`, `next week`, `February 15th`, `3/15/2023`, leap-day validity, Jan 31 month shifting, crossing Dec/Jan with a fixed offset, invalid/missing bases for relative and yearless dates, and two unrelated dates where only the first interval is returned.

- [ ] **Step 2: Replace point candidates with span-aware parsed expressions**

Introduce an internal immutable value with source positions and interval precision:

```python
@dataclass(frozen=True)
class _TemporalMatch:
    start: int
    end: int
    occurred_start: datetime
    occurred_end: datetime | None
```

Build absolute and relative match collectors. Date-only matches end at the next local midnight, explicit clock times have no end, and week expressions use Monday boundaries.

- [ ] **Step 3: Add English month and numeric absolute parsing**

Recognize full/abbreviated English month names with optional ordinal suffix and optional year, plus US `month/day/year`. Validate through `datetime`; only yearless forms require a valid event base.

- [ ] **Step 4: Add explicit range composition and disambiguation**

Detect `from <left> to <right>` and `between <left> and <right>`. Select one parsed expression wholly inside each side, return the left start and right exclusive end, and reject reversed ranges. Without an explicit connector, return only the earliest non-overlapping expression.

- [ ] **Step 5: Update extractor integration expectations**

Change existing date-only assertions so `occurred_end` is the next local midnight. Leave explicitly timed legacy schema fields untouched.

- [ ] **Step 6: Stage T4 changes without executing tests**

Review test sources and implementation with `git diff --check`; pytest execution remains delegated to CI.

### Task 2: B1 persona canonicalization and historical migration

**Files:**
- Modify: `src/hl_mem/domain/entity.py`
- Modify: `src/hl_mem/ingest/llm_extractor.py`
- Modify: `src/hl_mem/application/ingest.py`
- Create: `src/hl_mem/storage/migrations/038_subject_canonicalization.sql`
- Create: `src/hl_mem/storage/migrations/backfill_subject_canonicalization.py`
- Modify: `src/hl_mem/storage/database.py`
- Modify: `tests/unit/test_entity.py`
- Create: `tests/unit/test_backfill_subject_canonicalization.py`
- Modify: `tests/unit/test_claim_temporal_entities.py`

**Interfaces:**
- Consumes: `normalize_entity_id(subject, aliases=None) -> str`, `compute_fact_hash_v2`, `compute_conflict_key`.
- Produces: `PERSONA_ENTITY_ALIASES`, merged default/custom alias loading, and `backfill_subject_canonicalization(connection) -> int`.

- [ ] **Step 1: Add persona and namespace isolation tests before implementation**

Parameterize `我`, `本人`, `I`, `Ｉ`, `ME`, `myself`, `user`, `ＵＳＥＲ`, `the user`, `用户`, and `当前用户` to expect `user`. Assert named people/products remain distinct. Store equivalent persona claims in two tenant IDs and assert two claims survive with `subject_entity_id='user'` and different namespace-scoped conflict keys.

- [ ] **Step 2: Add strict persona aliases and merge custom aliases over defaults**

Define a separate persona mapping and include it in `DEFAULT_ENTITY_ALIASES`. Keep NFKC, whitespace folding, and casefold on lookup keys. When an aliases JSON file is configured, merge it over defaults rather than replacing built-ins.

- [ ] **Step 3: Normalize both extraction paths immediately**

Make compact subject postprocessing resolve known aliases to `user` while preserving unrecognized display spelling, and remove the legacy/compact branch that skips alias normalization in `_claim`. Keep the application ingest normalization as an idempotent boundary guard.

- [ ] **Step 4: Implement migration 038**

Register a no-op SQL marker, then run an idempotent Python migration after marker 038. For rows whose subject is an explicit persona alias, update subject, fact hash, v3 conflict key, and only the leading subject segment of `index_text`. Preserve claim IDs, states, evidence links, and unrelated entities.

- [ ] **Step 5: Add migration tests**

Seed claims in multiple namespaces with persona and named subjects. Assert persona rows become `user`, keys are recomputed per namespace, FTS-visible `index_text` changes, named subjects stay byte-for-byte unchanged, and the second migration call returns zero.

- [ ] **Step 6: Stage B1 changes without executing tests**

Use `git diff --check` and inspect the data migration transaction/rollback paths manually.

### Task 3: B2 extractor and MemDaily cache isolation

**Files:**
- Modify: `src/hl_mem/ingest/llm_extractor.py`
- Modify: `evaluation/tools/run_memdaily_benchmark.py`
- Modify: `tests/unit/test_llm_extractor.py`
- Modify: `tests/unit/test_memdaily_perltqa_benchmark_scripts.py`

**Interfaces:**
- Produces: `LANGUAGE_ROUTER_VERSION`, `_case_manifest_path`, `_cache_identity`, `_validate_cached_ingest` helpers.
- Consumes: `LLM_EXTRACTOR_VERSION`, `MemDailyTrajectory`, `Settings`, an open SQLite connection.

- [ ] **Step 1: Add fingerprint and cache validation tests before implementation**

Assert the language router version appears in postprocessing identity and that changing the supplied router version changes `compute_prompt_hash`. For MemDaily, assert a matching manifest plus matching DB claim versions is reusable, while a missing/corrupt/stale manifest or stale DB claim version returns a reason requiring re-ingest.

- [ ] **Step 2: Version the language router**

Add `LANGUAGE_ROUTER_VERSION = "language-router-v1"`, include it in `_postprocess_rules_fingerprint`, and accept a keyword-only override in `compute_prompt_hash` for deterministic testing.

- [ ] **Step 3: Add per-case cache manifests**

Derive a stable case fingerprint from IDs, namespace, message timestamps/text, and gold-independent ingest inputs. Manifest identity includes schema version, case fingerprint, extractor model/version, embedder model/dimension, and index text mode/version. Write atomically only after successful ingest.

- [ ] **Step 4: Validate manifest and DB extractor versions**

Read and compare the manifest without raising on ordinary staleness. Query distinct non-null `claims.extractor_version`; any value other than `LLM_EXTRACTOR_VERSION` invalidates the cache. Empty claim sets are accepted only with a matching manifest.

- [ ] **Step 5: Make skip-ingest refresh stale caches**

Before opening for reuse, validate the manifest identity. After opening, validate DB versions. On either failure, close/remove the case artifacts and perform normal ingest. Record `cache_status` and `cache_reason` as backward-compatible fields in the ingest result.

- [ ] **Step 6: Persist the actual extractor version during MemDaily ingest**

Add `extractor='llm'` and `extractor_version=getattr(extractor, 'extractor_version', LLM_EXTRACTOR_VERSION)` to the event passed to `store_extracted`.

### Task 4: Static verification, review, commit, push, and CI

**Files:**
- Review all files changed in Tasks 1-3.

**Interfaces:**
- Produces: committed `main`, pushed origin state, and CI status report.

- [ ] **Step 1: Format changed Python files**

Run black and isort in write mode only if checks indicate formatting differences, then rerun their check modes.

- [ ] **Step 2: Run permitted static checks**

Run:

```powershell
.venv/Scripts/python.exe -m black --check <changed-python-files>
.venv/Scripts/python.exe -m isort --check-only <changed-python-files>
.venv/Scripts/python.exe -m ruff check <changed-python-files>
.venv/Scripts/python.exe -m mypy src/hl_mem/
```

Report each exact result. Do not substitute pytest, benchmark, or canary execution.

- [ ] **Step 3: Review scope and repository state**

Run `git diff --check`, inspect `git diff --stat` and `git status --short`, and confirm the four pre-existing untracked files remain untouched.

- [ ] **Step 4: Commit implementation**

Stage only T4/B1/B2 sources, tests, migration, and plan. Commit with a focused message such as `feat: complete temporal subject and cache isolation`.

- [ ] **Step 5: Push main and inspect CI**

Push `main` to its configured upstream. Use the repository's available CLI or remote page to identify the workflow run for the pushed commit and report success, failure, pending, or an explicit inability to verify.
