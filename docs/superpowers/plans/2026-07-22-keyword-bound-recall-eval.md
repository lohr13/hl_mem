# Keyword-Bound Recall Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deterministic offline M6 recall evaluation suite whose labels bind claim and evidence IDs from a frozen SQLite snapshot by content keywords.

**Architecture:** Store human-readable JSONL cases with keyword bindings instead of unstable database IDs. A loader validates cases, a binder resolves unique claims and evidence from a read-only snapshot, metrics score structured recall results, and a runner emits an auditable JSON report. Pytest fixtures exercise the complete flow without external APIs.

**Tech Stack:** Python 3.11, SQLite, FastAPI TestClient/httpx, pytest.

## Global Constraints

- Runtime paths and real-API behavior are configured through CLI options or environment variables.
- The source snapshot is opened read-only and is never overwritten.
- Default tests are deterministic and make no external API calls.
- Keyword matching is explicit (`all` or `any`); one group may bind multiple relevant claims, and missing groups fail with actionable errors.
- Public modules, classes, and functions use Chinese docstrings and typed signatures.

---

### Task 1: Dataset model and dynamic binder

**Files:**
- Create: `tests/eval/dataset.py`
- Create: `tests/eval/test_dataset_schema.py`
- Create: `tests/eval/datasets/recall_v2.jsonl`

**Interfaces:**
- Produces: `EvalCase`, `load_cases(path)`, `bind_cases(connection, cases)`.

- [ ] Write schema and binding tests for valid cases, malformed cases, single/multiple matches, missing matches, and evidence resolution.
- [ ] Run `uv run pytest tests/eval/test_dataset_schema.py -v` and confirm the import/behavior failures.
- [ ] Implement strict JSONL validation and normalized keyword binding over claim subject, predicate, value, qualifiers, and linked event text.
- [ ] Re-run the focused tests and confirm they pass.

### Task 2: Metrics and report runner

**Files:**
- Create: `tests/eval/metrics.py`
- Create: `tests/eval/runner.py`
- Create: `tests/eval/test_metrics.py`
- Create: `tests/eval/test_runner.py`

**Interfaces:**
- Consumes: bound `EvalCase` values and recall response dictionaries.
- Produces: `evaluate_results(...)`, `aggregate_metrics(...)`, `run_evaluation(...)`, and JSON report output.

- [ ] Write failing tests for Recall@5, micro recall, top-1, no-answer precision/recall, stale/disputed rate, evidence correctness, temporal violations, and report persistence.
- [ ] Run focused tests and verify expected missing-function failures.
- [ ] Implement metric calculations with zero-denominator behavior and per-query diagnostics.
- [ ] Implement a callable/CLI runner that records source hash, configuration, latency, per-query output, and aggregate metrics.
- [ ] Re-run focused tests and confirm they pass.

### Task 3: Frozen fixture and end-to-end pytest coverage

**Files:**
- Create: `tests/eval/conftest.py`
- Create: `tests/eval/fixtures/build_snapshot.py`
- Create: `tests/eval/test_recall_eval.py`
- Create: `tests/eval/test_temporal_eval.py`
- Create: `tests/eval/test_no_answer_eval.py`
- Create: `tests/eval/README.md`
- Create: `tests/eval/reports/.gitkeep`
- Modify: `.gitignore`

**Interfaces:**
- Produces: pytest `--eval-db`, `--eval-report`, snapshot builder CLI, deterministic seeded snapshot fixtures.

- [ ] Write failing end-to-end tests for keyword binding, API contract (`observations=[]`), current/historical visibility, and no-answer scoring.
- [ ] Run `uv run pytest tests/eval/ -v` and verify failures are caused by missing fixtures/implementation.
- [ ] Implement the seeded frozen snapshot, read-only guard, TestClient fixture, CLI options, and report hook.
- [ ] Add usage and real-API isolation documentation; ignore generated reports while retaining `.gitkeep`.
- [ ] Run `uv run pytest tests/eval/ -v` and confirm all evaluation tests pass.

### Task 4: Full verification

**Files:**
- Modify only files required by failures introduced by Tasks 1-3.

- [ ] Run `uv run pytest tests/unit/ -v`.
- [ ] Run `uv run pytest tests/integration/ -v`.
- [ ] Run `uv run pytest tests/ -v`.
- [ ] Run `uv run python -m compileall -q src tests/eval`.
- [ ] Review `git diff --check` and `git status --short` for unintended files.
