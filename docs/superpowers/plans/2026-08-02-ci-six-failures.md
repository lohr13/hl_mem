# CI Six Failures Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the GitHub Actions `test.yml` workflow pass without private evaluation artifacts or API keys.

**Architecture:** Keep private evaluation artifacts outside Git and make their checks explicitly optional when absent. Keep integration coverage deterministic by seeding claims through `ClaimRepository`, the write boundary responsible for tokenized FTS v2 synchronization. Make config-loader coverage self-contained by using an explicit empty dotenv path and disabling the optional network-backed query expander.

**Tech Stack:** Python 3.11+, pytest, SQLite FTS5, FastAPI TestClient, GitHub Actions YAML.

## Global Constraints

- Do not add or modify private evaluation data.
- Preserve unrelated worktree changes.
- Use fake/off components in tests that do not exercise real providers.
- Verify the six reported tests, all unit tests, and remote `test.yml`.

---

### Task 1: Optional private evaluation artifacts

**Files:**
- Modify: `tests/eval/test_ci_fixture_hash.py`
- Modify: `tests/eval/test_dataset_schema.py`
- Modify: `.github/workflows/test.yml`

**Interfaces:**
- Consumes: ignored `tests/eval/datasets/` and `tests/eval/baselines/` artifacts when locally present.
- Produces: pytest skips and a successful, explicitly reported recall-gate skip when private artifacts are absent.

- [ ] **Step 1: Use the existing CI failures as the red test**

Run the two reported eval tests and `python -m tests.eval.ci_gate`; confirm missing private paths fail.

- [ ] **Step 2: Mark only the two private-artifact tests as conditional**

Add `pytest.mark.skipif` decorators that name the missing private fixture and leave all synthetic eval tests active.

- [ ] **Step 3: Make the standalone workflow gate conditional on all three private artifacts**

Use `hashFiles` checks for `recall_v2.jsonl`, its manifest, and `baseline_v019_ci.json`; add a reporting step for the skipped case.

- [ ] **Step 4: Run the two eval tests**

Expected: two skips and zero failures when the private directories are absent.

### Task 2: Repository-backed integration seeds

**Files:**
- Modify: `tests/integration/test_context_packet_feedback.py`
- Modify: `tests/integration/test_hermes_delivery.py`
- Modify: `tests/integration/test_p0p1_integration.py`

**Interfaces:**
- Consumes: `ClaimRepository.insert_claim(claim: dict[str, Any], commit: bool = True) -> bool`.
- Produces: source claim rows and matching tokenized FTS v2 rows in one transaction.

- [ ] **Step 1: Use the three reported integration failures as the red test**

Run the exact tests and confirm recall returns empty materialized items.

- [ ] **Step 2: Replace raw claim INSERT setup with `ClaimRepository` writes**

Preserve claim identifiers, stored values, timestamps, scopes, expiry, and explicit `index_text`; use `commit=False` where setup has more writes, then commit once.

- [ ] **Step 3: Run the three integration tests**

Expected: all three pass and continue asserting receipt/feedback/TTL behavior.

### Task 3: Hermetic config-loader native-type test

**Files:**
- Modify: `tests/unit/test_config_loader.py`

**Interfaces:**
- Consumes: `load_settings(config_path, env_path, environ={})`.
- Produces: a config-only native-type test that never reads the repository `.env` or enables a network component.

- [ ] **Step 1: Reproduce the CI error with an absent dotenv path**

Confirm the default `query_expansion_mode='auto'` requires `LLM_API_KEY`.

- [ ] **Step 2: Disable query expansion in the test TOML and pass an explicit absent dotenv path**

Keep `query_expansion_model='glm-4.7'` so string coercion remains covered.

- [ ] **Step 3: Run the config-loader test**

Expected: pass with an empty process environment and no dotenv file.

### Task 4: Verification and delivery

**Files:**
- Verify all modified files and preserve unrelated untracked files.

**Interfaces:**
- Consumes: pytest, Ruff/format checks as applicable, Git, `gh` CLI.
- Produces: one pushed commit and a successful `test.yml` run for its SHA.

- [ ] **Step 1: Run the six reported tests together**

Expected: four passes, two intentional skips, zero failures.

- [ ] **Step 2: Run all unit tests**

Run `uv run --frozen python -m pytest tests/unit/ -q --tb=short` with inherited `PYTHONPATH` removed.

- [ ] **Step 3: Run focused lint/format checks and inspect the diff**

Ensure no private artifact or unrelated untracked file is staged.

- [ ] **Step 4: Commit and push**

Commit message: `fix(ci): repair 6 failing tests for remote CI environment`.

- [ ] **Step 5: Watch the workflow to completion**

Use `gh run watch` for the pushed SHA and inspect any failed job before claiming success.
