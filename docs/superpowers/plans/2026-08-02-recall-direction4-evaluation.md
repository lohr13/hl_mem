# Recall Direction 4 Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and run a frozen 28-case off-versus-auto recall evaluation while keeping sensitive artifacts outside Git.

**Architecture:** Extend only `tests/eval/eval_runner.py` and its focused tests. The runner loads the normal TOML/dotenv configuration, overrides query expansion per run, copies the frozen snapshot before opening the API, records result-level dense cosine and reranker raw scores, and aggregates top-result distributions by answerability. The dataset, snapshot, JSON outputs, and Markdown comparison live under `~/hl_mem_eval_data/`.

**Tech Stack:** Python 3.11+, pytest, FastAPI TestClient, SQLite backup API, existing HL-Mem embedding/reranker clients.

## Global Constraints

- Use exactly 28 cases: 18 answerable and 10 no-answer.
- Cover `colloquial`, `coreference`, `distractor`, and `no_answer`; variants of one fact share `pair_id`.
- Freeze `var/hl_mem.db` before the first evaluation query and never query-write the source snapshot.
- Use the same frozen snapshot and real embedding/reranker configuration for `off` and `auto`.
- Keep datasets, snapshots, JSON results, and Markdown report under `~/hl_mem_eval_data/`; do not add them to Git.
- Do not enter an interactive Python REPL.

---

### Task 1: Runner contract via TDD

**Files:**
- Modify: `tests/eval/test_recall_v2_gate.py`
- Modify: `tests/eval/eval_runner.py`

**Interfaces:**
- Consumes: dataset rows containing optional `pair_id`; debug responses containing `search_trace.candidates` and result `reranker_raw_score`.
- Produces: `_score(..., dense_raw_scores: dict[str, float] | None)` query records, `_distribution(values)` summaries, and `report["score_distributions"]` grouped by `answerable`/`no_answer`.

- [x] **Step 1: Write failing tests**

  Add behavior tests proving `pair_id` survives, each returned claim records `dense_raw_score` and `reranker_raw_score`, empty distributions use `count=0` plus null statistics, nearest-rank percentiles are correct, and CLI config/expansion arguments reach `run()`.

- [x] **Step 2: Run tests to verify RED**

  Run: `$env:PYTHONPATH=(Resolve-Path 'src').Path; .venv/Scripts/python.exe -m pytest tests/eval/test_recall_v2_gate.py -q --tb=short`

  Expected: failures because the new parameters, score records, distribution helper, and CLI options do not exist.

- [x] **Step 3: Implement the minimal runner extension**

  Load settings with `load_settings(config_path, env_path)`, apply `dataclasses.replace(..., query_expansion_mode=mode)`, embed all original queries once per run, calculate exact cosine against returned claim BLOBs in the disposable database copy, and collect reranker scores from trace with API fallback. Aggregate the first returned claim's raw scores into answerable/no-answer distributions with `count/min/p10/p25/p50/p75/p90/max/mean`.

- [x] **Step 4: Run tests to verify GREEN**

  Run the focused command from Step 2 and then `tests/eval/ -q --tb=short` excluding external live-data tests only if their own skip markers apply.

---

### Task 2: Frozen external evaluation assets

**Files:**
- Create outside Git: `~/hl_mem_eval_data/snapshot/hl_mem_phase2_direction4_20260802.db`
- Create outside Git: `~/hl_mem_eval_data/snapshot/manifest_phase2_direction4_20260802.json`
- Create outside Git: `~/hl_mem_eval_data/datasets/recall_phase2_direction4_28.jsonl`

**Interfaces:**
- Consumes: active claims in the frozen snapshot.
- Produces: explicit `expected_claim_ids`, empty no-answer IDs, and stable `pair_id` labels.

- [x] **Step 1: Freeze source with the existing builder**

  Run `tests/eval/fixtures/build_snapshot.py` once before any evaluation query, then verify manifest count/hash and SQLite integrity.

- [x] **Step 2: Materialize and validate 28 JSONL rows**

  Create six answerable fact groups with three variants each (`colloquial`, `coreference`, `distractor`) plus ten `no_answer` rows. Validate unique IDs, exact slice counts, six answerable pair IDs with three rows each, explicit IDs present and active in the snapshot, and no query text present in claim index text.

---

### Task 3: Real off/auto evaluation and report

**Files:**
- Create outside Git: `~/hl_mem_eval_data/reports/phase2_direction4_off.json`
- Create outside Git: `~/hl_mem_eval_data/reports/phase2_direction4_auto.json`
- Create outside Git: `~/hl_mem_eval_data/phase2_direction4_report_20260802.md`

**Interfaces:**
- Consumes: identical snapshot/dataset/config/env paths; `--expansion-mode off|auto`.
- Produces: comparable ranking, no-answer, latency, expansion-path, and score-distribution results.

- [x] **Step 1: Run off and auto**

  Invoke the runner twice with the same snapshot, dataset, `hl_mem.toml`, `.env`, top-k, and reference time; vary only expansion mode.

- [x] **Step 2: Validate comparability and summarize**

  Assert identical dataset/snapshot SHA-256 values, real embedder/reranker health, 28 HTTP 200 responses, and matching case/slice counts. Write a Markdown table for Recall@1, MRR, no-answer precision/recall, latency, expansion usage, and raw-score distributions.

---

### Task 4: Verification and commit

**Files:**
- Commit only repository changes for the runner, focused tests, and this implementation plan.

**Interfaces:**
- Produces: one Git commit whose message states the evaluation runner capability.

- [x] **Step 1: Verify**

  Run focused eval tests, all unit tests, formatting/lint checks for changed Python files, `git diff --check`, external artifact validation, and `git status --short`.

- [x] **Step 2: Commit**

  Stage only `tests/eval/eval_runner.py`, its focused test file, and this plan. Preserve all pre-existing untracked user files.
