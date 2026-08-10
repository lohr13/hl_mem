# LongMemEval Reader Recorded-Time Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent benchmark ingestion timestamps from causing evidence-rich LongMemEval reader calls to abstain.

**Architecture:** Keep recorded time in benchmark storage and diagnostic reports, but remove it from the evaluation-only
reader prompt projection. Preserve occurred/valid time and the temporal-specific baseline rules, then bump the reader
context protocol so incompatible resume reports cannot be mixed.

**Tech Stack:** Python 3.11+, unittest-style pytest collection, SQLite, LongMemEval runner.

## Global Constraints

- Do not change production reader/recall behavior under `src/hl_mem/`.
- Do not change datasets, retrieval thresholds, or ingest timestamps.
- Keep the reader prompt concise.
- Do not run pytest locally; use GitHub CI after push.
- Preserve user-owned untracked files.

---

### Task 1: Reader prompt regression coverage

**Files:**
- Modify: `tests/unit/test_longmemeval_batching.py`

**Interfaces:**
- Consumes: `runner._reader_system_prompt()` and `runner._build_reader_user_prompt()`.
- Produces: observable assertions for benchmark reader time projection.

- [ ] **Step 1: Extend the existing reader-context fixture**

Assert the rendered claim contains literal `valid_from`, the rendered event contains literal `occurred_at`, and neither
record contains `recorded_from`, `recorded_to`, or `recorded_at`. Add the same event-field assertion to the assistant raw
fallback fixture.

- [ ] **Step 2: Cover system prompt temporal preservation**

Assert the generic prompt names occurred/valid time but not recorded time. Keep the existing temporal assertions for the
effective baseline, relative offset, and no-later-current-value rules.

- [ ] **Step 3: Defer execution to CI**

Do not run pytest locally, as explicitly requested. The pre-fix mutation is known: current reader records serialize all
three recorded fields and the system prompt says `occurred, valid, and recorded times`, so the new assertions fail before
implementation.

### Task 2: Evaluation-only projection fix and protocol documentation

**Files:**
- Modify: `evaluation/tools/longmemeval/reader_context.py`
- Modify: `evaluation/tools/run_longmemeval_benchmark.py`
- Modify: `evaluation/README.md`

**Interfaces:**
- Consumes: retrieved claim dictionaries and SQLite event rows with full dual-time metadata.
- Produces: reader JSON with valid/occurred fields only; `READER_CONTEXT_PROTOCOL_VERSION = "session-turn-window-v2"`.

- [ ] **Step 1: Remove recorded fields at the prompt projection boundary**

Delete `recorded_from`/`recorded_to` from `reader_claim_records()` and `recorded_at` from `_event_record()` and
`load_assistant_raw_fallback()`. Leave database queries and `_retrieved_payload()` unchanged so diagnostics do not regress.

- [ ] **Step 2: Simplify the system prompt time instruction**

Replace `use occurred, valid, and recorded times plus Current Date` with `use occurred and valid times plus Current Date`.
Do not add a benchmark caveat sentence.

- [ ] **Step 3: Bump and document the reader protocol**

Set the protocol to v2. Document that v1 resume reports are incompatible while cached ingest databases/manifests remain
reusable because no persisted or extraction data changed.

### Task 3: Verification, review, and delivery

**Files:**
- Verify only: the modified source, tests, docs, and fixed benchmark artifacts.

**Interfaces:**
- Consumes: fixed databases and retrieved Top-10 snapshots for `1d4e3b97` and `60d45044`.
- Produces: reader answers, review findings, commit, push, and CI status.

- [ ] **Step 1: Run non-pytest checks**

Run compile/format/lint checks available through the repository launcher. Inspect the final diff and confirm no production
files, thresholds, datasets, caches, or user-owned untracked files changed.

- [ ] **Step 2: Re-run only the fixed-evidence reader calls**

Build v2 prompts from each fixed DB and stored retrieved list, call the configured QA model with ingestion timestamps
absent, and record whether each answer recovers from `unavailable`. Do not run extraction or retrieval again.

- [ ] **Step 3: Review, commit, push, and inspect CI**

Request an independent code review, resolve material findings, commit only task files, push the current branch, and inspect
the triggered GitHub Actions run. Mark any provider or CI result that cannot be observed as a warning.
