# HL-Mem All Phases Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the Phase 0–6 target architecture while retaining SQLite as the default backend and representing procedures inside `policies.procedure`.

**Architecture:** Phase 0 remains intentionally skipped. Existing Phase 1–2 behavior is preserved and regression-tested; Phase 3 adds evidence-backed derivation maintenance, Phase 4 adds the experience/policy channel, Phase 5 adds routed multi-channel recall plus MCP/CLI management, and Phase 6 adds production safeguards and an optional PostgreSQL adapter boundary without replacing SQLite WAL.

**Tech Stack:** Python 3.11, FastAPI, SQLite WAL/FTS5, optional PostgreSQL driver, pytest.

## Global Constraints

- Do not create a `procedures` table; store a procedure document and its lifecycle/reliability fields in `policies`.
- Every active derivation and policy must have evidence links.
- Runtime paths, limits, timeouts, model names, and credentials come from environment variables or configuration.
- SQLite WAL remains the default and all default tests are offline.
- Public modules, classes, and functions use Chinese docstrings and Python type annotations.

---

### Task 1: Phase 3 derived-memory maintenance

**Files:** migration `008_derived_memory.sql`, `storage/repository.py`, `workers/mental_models.py`, `workers/worker.py`, unit/integration tests.

- [ ] RED: test proof counts, watermarks, stale dependency propagation, idempotent rebuild, lease recovery, and evidence admission.
- [ ] GREEN: add derivation metadata/repository operations and the incremental maintenance worker.
- [ ] VERIFY: run focused and existing worker/forget suites.

### Task 2: Phase 4 experience and policy channel

**Files:** migration `009_experience.sql`, `experience/models.py`, `experience/service.py`, `storage/repository.py`, tests.

- [ ] RED: test episode/trace assembly, reward attribution, independent support, policy activation, procedure probation, outcome updates, retirement, and evidence lineage.
- [ ] GREEN: implement episodes, traces, feedback, and policies with embedded procedure JSON; do not add a procedures table.
- [ ] VERIFY: run schema, lifecycle, and repository tests.

### Task 3: Phase 5 routed recall and management surfaces

**Files:** `recall/router.py`, `recall/extended_pipeline.py`, `mcp/server.py`, `cli.py`, `api/server.py`, tests.

- [ ] RED: test router intents, Fact/Temporal/Relation/Procedure channels, RRF/MMR, budget packing, scope isolation, explain, MCP contracts, and CLI export/import.
- [ ] GREEN: implement deterministic routing and adapters over repository interfaces.
- [ ] VERIFY: run API, MCP, CLI, recall, and scope suites.

### Task 4: Phase 6 production boundaries

**Files:** `storage/base.py`, `storage/postgres.py`, `storage/backup.py`, `security/retention.py`, migration/tests/docs.

- [ ] RED: test adapter contract, backup/restore manifest validation, tenant quotas, retention, and migration rehearsal.
- [ ] GREEN: preserve SQLite defaults, add optional PostgreSQL adapter loading and production maintenance primitives.
- [ ] VERIFY: run adapter contract tests without requiring an external PostgreSQL server.

### Task 5: Documentation and full verification

**Files:** `docs/architecture.md`, `docs/implementation-plan.md`, `docs/CHANGELOG.md`, relevant README/config docs.

- [ ] Remove obsolete “not implemented” annotations for delivered features and document `policies.procedure`.
- [ ] Run `uv run pytest tests/unit/ -v`, integration/scenario/eval suites, full pytest, compileall, and `git diff --check`.
