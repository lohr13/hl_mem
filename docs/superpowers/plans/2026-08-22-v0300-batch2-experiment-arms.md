# v0.30.0 Batch 2 Experiment Arms Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build offline-only state canonicalization/atomicity experiment arms, a structured protocol scorer, and a frozen 400-bundle counterexample corpus in exactly two commits.

**Architecture:** Keep deterministic gates and arm file I/O in one evaluation module, scoring and read-only persisted-edge loading in a second, and corpus generation/read-only redaction in a third. Production packages never import these modules; all identities are structural and all database scoring reads real claim/evidence columns.

**Tech Stack:** Python 3.11, dataclasses, JSON/JSONL, SQLite URI read-only mode, pytest.

## Global Constraints

- Work on `main`; preserve the three allowed untracked files and do not edit `hl_mem.toml`.
- Produce exactly two commits with Chinese messages.
- Do not add runtime settings, tables, columns, service operations, or real LLM calls.
- Predicate is not part of `StateCoordinate`; call `coordinate_qualifier_key()` and decode frozen qualifier JSON once.
- Corpus is 280 dev plus 120 sealed, 50% deidentified real-source plus 50% adversarial synthetic, with 1000 events total.
- After sealing, do not inspect sealed content; only scorer aggregation and byte-level manifest verification may consume it.

---

### Task 1: Canonicalization gate

**Files:**
- Create: `tests/unit/test_state_experiment_arms.py`
- Create: `src/hl_mem/evaluation/state_experiment_arms.py`

**Interfaces:**
- Consumes: v0.29.3 response objects shaped as `{"claims": [compact claim], "should_memorize": bool}`.
- Produces: `canonicalize_claim(raw_claim, namespace="default") -> dict[str, object]` and a serializable coordinate projection.

- [ ] Write literal fixtures for version, service health, predicate drift and counterexample non-state claims; assert exact coordinate dictionaries.
- [ ] Run `.venv/Scripts/python.exe -m pytest tests/unit/test_state_experiment_arms.py -q` and confirm import failure.
- [ ] Implement fixed kind routing, canonical subject normalization, slot detection, qualifier extraction and one-time `StateCoordinate` serialization.
- [ ] Re-run the focused test and confirm pass.

### Task 2: Atomicity gate and arm runner

**Files:**
- Modify: `tests/unit/test_state_experiment_arms.py`
- Modify: `src/hl_mem/evaluation/state_experiment_arms.py`

**Interfaces:**
- Produces: `apply_atomicity_gate(raw_claim, strategy) -> dict[str, object]`, `run_arm(samples, arm, ...)`, and `run_arm_file(...)`.
- B2 consumes an optional callable; missing callable raises `NotImplementedError`.

- [ ] Add failing tests for one-state passthrough, two-state split, reject, A byte-structure passthrough, stable assertion IDs and B2 fail closed.
- [ ] Run the focused tests and confirm missing APIs fail.
- [ ] Implement clause segmentation, state assertion counting, `split|reject`, A/B1 dispatch and atomic JSONL output replacement.
- [ ] Re-run focused tests; mentally mutate arm dispatch and split ordering and ensure assertions would fail.

### Task 3: Structured protocol scorer

**Files:**
- Create: `tests/unit/test_state_experiment_scoring.py`
- Create: `src/hl_mem/evaluation/state_experiment_scoring.py`

**Interfaces:**
- Produces: set metrics, three-run coordinate consistency, `load_persisted_edges(db_path, manifest)`, and `score_protocol(...)`.
- Reads only `claims.id/superseded_by_id` and structured `evidence_links` for actual supersede edges.

- [ ] Add failing tests covering coordinate/atomic precision and recall, edge union from both real columns, cross-coordinate zero, stale reduction/absolute rate, historical recall, non-state F1 delta, inflation and consistency.
- [ ] Build a temporary SQLite fixture whose audit text contains a fake `snapshot_advance`; assert it is ignored.
- [ ] Run the scorer tests and confirm import failure.
- [ ] Implement metric helpers, read-only edge loading, fixed threshold table and per-metric pass/fail report.
- [ ] Re-run scorer and arm tests.

### Task 4: Commit experiment equipment

**Files:**
- Modify: `src/hl_mem/evaluation/state_lifecycle.py` only to replace the compatibility projection call with `coordinate_qualifier_key()`.
- Add: this design and plan.

- [ ] Run black/isort/ruff on Commit 6 files and the focused unit tests.
- [ ] Grep `src/hl_mem/application` and `src/hl_mem/domain` for imports of the new modules; expect zero.
- [ ] Review `git diff --check` and staged diff.
- [ ] Commit as `eval: 新增状态规范化与原子性实验臂`.

### Task 5: Corpus and read-only sampler

**Files:**
- Create: `tests/unit/test_state_counterexample_corpus.py`
- Create: `src/hl_mem/evaluation/state_counterexample_corpus.py`
- Create: `evaluation/tools/sample_state_events.py`
- Create: `evaluation/tools/generate_state_counterexample_corpus.py`

**Interfaces:**
- Produces: `sample_redacted_seeds(source_db, limit, seed)`, `generate_corpus(seeds, output_dir)`, and CLI wrappers requiring explicit paths.

- [ ] Write failing tests for URI read-only enforcement, closed-lexicon redaction, deterministic output, exact split/category/source/event totals and gold/corpus separation.
- [ ] Run the focused corpus tests and confirm missing module failure.
- [ ] Implement a query-only sampler and fixed template generator with explicit gold annotations independent of candidate gate helpers.
- [ ] Re-run focused tests and a deterministic regeneration comparison.

### Task 6: Freeze corpus and Commit 7

**Files:**
- Force-add: `evaluation/datasets/v0300_state_dev_corpus.jsonl`
- Force-add: `evaluation/datasets/v0300_state_dev_gold.jsonl`
- Force-add: `evaluation/datasets/v0300_state_sealed_corpus.jsonl`
- Force-add: `evaluation/datasets/v0300_state_sealed_gold.jsonl`
- Force-add: `evaluation/datasets/v0300_state_corpus_manifest.json`

- [ ] Run the sampler against an explicitly passed read-only source into a Temp seed file; never edit the source database.
- [ ] Generate all frozen files once, verify exact aggregate counts, SHA-256 and gold coverage, then declare sealed content closed.
- [ ] Run corpus tests without parsing committed sealed files.
- [ ] Review staged stats and commit as `eval: 冻结状态反例语料`.

### Task 7: Full verification and report

**Files:** No new files.

- [ ] Run black, isort, ruff, mypy and `scripts/check_docs_consistency.py`.
- [ ] Run batch 0 targeted tests, batch 1 added tests, all new focused tests, then the required full unit command with clean environment variables and `-p no:langsmith`.
- [ ] Grep production packages for new evaluation imports and inspect `git status` for only the three allowed untracked files.
- [ ] Record both commit hashes, changed-file lists, dev aggregates, sealed count only, threshold mapping, unverified items and next-round request estimate.

## Plan self-review

Every frozen requirement maps to a task. Signatures and file names are consistent across tasks. There are no placeholder instructions; B2 fail-closed behavior is explicit. The plan intentionally uses inline execution because the user required main-branch work, two fixed commits, and no confirmation pause.
