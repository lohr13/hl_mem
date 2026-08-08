# Extraction Language, Episodic Memory, and Relative Time Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route extraction by source language, retain evidence-backed incidental details as bounded episodic claims, parse English relative dates from event time, and remove legacy template noise from the default index projection.

**Architecture:** Keep the compact six-field LLM contract and existing claim table. Add per-chunk bilingual prompt routing, project low-notability facts into temporal/ephemeral claims, isolate relative-time parsing in a pure module, and select the existing natural index projection as the runtime default with an explicit backfill path.

**Tech Stack:** Python 3.11+, Pydantic, SQLite/FTS5, unittest-compatible pytest tests, black, isort, ruff, mypy.

## Global Constraints

- Do not run benchmark, canary, or pytest on this Windows host.
- Keep RecallService and IngestService public signatures and return structures backward compatible.
- New LLM response fields are forbidden; compact output remains the existing optional-compatible six-field schema.
- Do not modify turn/span retrieval or reader synthesis.
- Existing unrelated untracked files must not be staged.

## File Map

- Create `src/hl_mem/ingest/relative_time.py`: pure absolute/relative date parsing against an explicit event timestamp.
- Modify `src/hl_mem/ingest/llm_extractor.py`: bilingual prompts, language routing, neutral kind projection, episodic post-processing, relative-time integration.
- Modify `src/hl_mem/ingest/admission.py`: admit safe low-notability episodic candidates with an auditable reason.
- Modify `src/hl_mem/domain/claims/retention.py`: use recorded time as the TTL anchor for ephemeral claims.
- Modify `src/hl_mem/settings.py`, `src/hl_mem/storage/claims.py`, `src/hl_mem/cli.py`: natural projection default and complete backfill mode support.
- Modify `README.md`, `docs/configuration.md`, `config.example.toml`: document the new default and explicit migration command.
- Create `tests/unit/test_extraction_language_episodic_time.py`: language, episodic, time, and TTL behavior.
- Modify `tests/unit/test_admission_unittest.py`, `tests/unit/test_extraction_prompt_quality.py`, `tests/unit/test_p1_8_settings_modes.py`: update old policy expectations.

---

### Task 1: Language routing and source-language output

**Files:**
- Modify: `src/hl_mem/ingest/llm_extractor.py`
- Test: `tests/unit/test_extraction_language_episodic_time.py`

**Interfaces:**
- Produces: `detect_extraction_language(text: str) -> Literal["zh", "en"]`
- Consumes: `ExtractionChunk.text`, the existing `LLMRequest`, and compact schema.

- [ ] **Step 1: Write failing behavior tests**

Add a recording fake client and assert that English chunks receive the English system/user prompt, Chinese chunks receive the Chinese prompt, English compact claims preserve `user` and English value text, and named subjects are not rewritten to user.

- [ ] **Step 2: Verify RED without pytest**

Run: `.venv/Scripts/python.exe tests/unit/test_extraction_language_episodic_time.py`

Expected: import/assertion failure because the English prompt/router does not exist.

- [ ] **Step 3: Implement the minimal route**

Define `ENGLISH_SYSTEM_PROMPT`, a Han-vs-Latin-word router, bilingual user wrappers and retry instructions. Replace `_KIND_MAP` predicate labels with neutral English values and normalize only at the canonical domain boundary. Normalize exact first-person subjects to `用户`/`user`; preserve all named subjects.

- [ ] **Step 4: Verify GREEN without pytest**

Run the same direct unittest file and require exit code 0.

### Task 2: Episodic admission and bounded retention

**Files:**
- Modify: `src/hl_mem/ingest/admission.py`
- Modify: `src/hl_mem/ingest/llm_extractor.py`
- Modify: `src/hl_mem/domain/claims/retention.py`
- Test: `tests/unit/test_extraction_language_episodic_time.py`
- Test: `tests/unit/test_admission_unittest.py`
- Test: `tests/unit/test_extraction_prompt_quality.py`

**Interfaces:**
- Consumes: `MemoryCandidate.notability`, compact kind, existing `ExtractedClaim.scope/volatility/importance`.
- Produces: reason `accepted_episodic` and temporal/ephemeral low-notability fact claims.

- [ ] **Step 1: Write failing behavior tests**

Assert that an evidence-backed IKEA four-hour detail is accepted, encoded as temporal/ephemeral with importance 0.3, while low-notability operational snapshots remain rejected. Assert ephemeral expiration uses `recorded_from` and stable temporal expiration still uses `observed_at`.

- [ ] **Step 2: Verify RED without pytest**

Run the direct unittest file and a small `python -c` admission assertion; require the new episodic assertion to fail for the old low-notability filter.

- [ ] **Step 3: Implement minimal dual-layer behavior**

Move enum validation before policy checks, remove the unconditional low rejection, return `accepted_episodic` after all safety/evidence checks, and override low `fact`/`plan` candidates to temporal/ephemeral. Update both prompts so low means episodic rather than discarded. In retention, select `recorded_from` only when volatility is ephemeral.

- [ ] **Step 4: Verify GREEN without pytest**

Run the direct unittest file and require exit code 0.

### Task 3: English and mixed relative dates

**Files:**
- Create: `src/hl_mem/ingest/relative_time.py`
- Modify: `src/hl_mem/ingest/llm_extractor.py`
- Test: `tests/unit/test_extraction_language_episodic_time.py`

**Interfaces:**
- Produces: `infer_occurrence(text: str, occurred_at: str | None) -> tuple[str | None, str | None]`
- Consumes: evidence quote and event `occurred_at`; never reads current wall-clock time.

- [ ] **Step 1: Write failing table-driven tests**

Use literal expected ISO values for yesterday, last week, three months ago, next Friday, Chinese `三个月前`, a mixed-language quote, an absolute date range, invalid/missing event time, and end-of-month clamping.

- [ ] **Step 2: Verify RED without pytest**

Run the direct unittest file and require import failure because `relative_time` is absent.

- [ ] **Step 3: Implement the pure parser**

Parse the explicit ISO base, absolute dates, fixed-day phrases, numbered offsets, week phrases, and qualified weekdays. Sort valid relative matches by source position, calendar-shift months/years with month-end clamping, preserve the base timezone, and return midnight ISO values.

- [ ] **Step 4: Integrate and verify GREEN**

Delegate `LLMExtractor._infer_compact_occurrence` to the pure function and run the direct unittest file with exit code 0.

### Task 4: Natural index default and migration path

**Files:**
- Modify: `src/hl_mem/settings.py`
- Modify: `src/hl_mem/storage/claims.py`
- Modify: `src/hl_mem/cli.py`
- Modify: `config.example.toml`
- Modify: `README.md`
- Modify: `docs/configuration.md`
- Modify: `tests/unit/test_p1_8_settings_modes.py`
- Modify: `tests/unit/test_answerable_index.py`

**Interfaces:**
- Produces: `Settings().index_text_mode == "natural"`, `index_text_version == "v2"`.
- Consumes: existing `build_index_text(..., mode="natural")` and `backfill_index_text` worker.

- [ ] **Step 1: Write failing defaults/CLI tests**

Update the settings default expectation and add a parser behavior assertion that `backfill-index-text --mode natural --dry-run` reaches the existing worker.

- [ ] **Step 2: Verify RED without pytest**

Run direct Python assertions against `Settings` and CLI parser; expect the old legacy default or restricted choices to fail.

- [ ] **Step 3: Implement and document**

Change Settings defaults to natural/v2, make repository fallback use its resolved settings mode, allow all four index modes in CLI, and document dry-run/apply migration commands. Do not auto-backfill or call a provider at startup.

- [ ] **Step 4: Verify GREEN without pytest**

Run direct Python assertions for Settings, natural projection output, and CLI parsing.

### Task 5: Static verification, review, commit, push, CI

**Files:**
- Review all files above.

**Interfaces:**
- Produces: a clean scoped commit on `main` and remote CI evidence.

- [ ] **Step 1: Format changed Python files**

Run black and isort on only the changed Python files, then run their check-only modes.

- [ ] **Step 2: Run required static checks**

Run `.venv/Scripts/black.exe --check <changed-python-files>`, `.venv/Scripts/isort.exe --check-only <changed-python-files>`, `.venv/Scripts/ruff.exe check <changed-python-files>`, and `.venv/Scripts/mypy.exe src/hl_mem/`.

- [ ] **Step 3: Review scope and mutation coverage**

Inspect `git diff --check`, `git diff --stat`, and the full diff. Confirm unrelated untracked files are absent, no retrieval files changed, and each wrong branch/date/TTL/default mutation is caught by a new test.

- [ ] **Step 4: Commit and push**

Stage only the scoped implementation/tests/docs, commit with `feat: optimize bilingual episodic extraction`, and run `git push origin main` without force.

- [ ] **Step 5: Confirm CI**

Use the repository forge CLI to locate the workflow run for the pushed SHA and wait for completion. Report the exact conclusion; mark unavailable or unverified checks with ⚠️.
