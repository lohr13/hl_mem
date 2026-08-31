# HL-Mem Core 1.0 Implementation Roadmap

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver HL-Mem 1.0 as a reliable single-machine memory product with governed Provider plugins, explicit model cost control, auditable automation, clean module boundaries, and release-grade evidence.

**Architecture:** Keep the existing modular monolith and SQLite authority model. Implement the approved design through six ordered, independently reviewable phases; every phase must leave a working product and pass its own gate before the next phase plan is written or executed.

**Tech Stack:** Python 3.12-3.14, FastAPI/Starlette ASGI, SQLite WAL/FTS5, httpx, Pydantic, uv, pytest, mypy, Ruff, Black, isort, GitHub Actions.

## Global Constraints

- Frozen functional baseline is `v0.36.1 / 2dbb6a9`; the approved design commit is `7619d7c`.
- SQLite remains the only authoritative memory store; do not add PostgreSQL, Neo4j, Graphiti, graph dual-write, distributed workers, multi-tenancy, or high availability.
- Production startup must fail clearly when an enabled high-quality Provider is unavailable; Fake implementations remain explicit test/development fixtures only.
- Stable 1.x plugin capabilities are LLM, Embedding, and Reranker; Image Describer remains experimental preview.
- Plugins are trusted in-process code, not a sandbox; external plugins load only from the explicit `[plugins].enabled` allowlist.
- Official Provider calls use host-owned transport and must pass through timeout, retry, error normalization, usage reservation, settlement, audit, and metrics.
- Relation discovery can produce audit Proposals only; no LLM path may directly create an official edge, dispute a Claim, or supersede a Claim.
- Query Expansion, Resurrection, LLM dedup, LLM reclassify, Policy induction, relation discovery, and semantic conflict consolidation default to off.
- Deterministic TTL/decay/archive/cleanup, stale propagation, Observation construction, near-copy review, L0 conflict repair, and Plan fulfillment remain automatic.
- Historical recall containing `as_of` or `known_as_of` must not inject current Policy or Derivation records.
- Coverage floor is 80%; full tests must produce zero `ResourceWarning`.
- Do not introduce pluggy, a DI framework, generic hooks, plugin routes/jobs/migrations/storage, `utils/common/services` buckets, or mechanical file-length rules.
- Do not perform a full-tree rename. Split only files touched by the phase when a responsibility boundary is demonstrated by tests.
- Use TDD for behavior changes: failing test, observed failure, minimal implementation, passing targeted test, relevant regression suite, then commit.
- Preserve unrelated user work. Never stage `.coverage`, `Temp/`, `docs/research/v028-plan-draft.md`, `hl_mem.toml.bak_0820`, or `nul`.
- Each task is one reviewable commit. Do not combine behavior migration, public contract changes, and structural refactors in one commit.

---

## Phase dependency graph

```text
Phase 1: Foundation correctness and safety
    ↓
Phase 2: Versioned configuration and upgrade experience
    ↓
Phase 3: Governed Provider transport, usage ledger, and plugin API
    ↓
Phase 4: Automatic behavior and relation governance
    ↓
Phase 5: Targeted architecture cleanup and evaluation separation
    ↓
Phase 6: Release gates, security automation, benchmark, and RC
```

No phase runs in parallel with a phase below it. Tasks inside a phase may run in parallel only when their declared files and interfaces do not overlap.

## Phase 1: Foundation correctness and safety

**Executable plan:** `docs/superpowers/plans/2026-08-30-hl-mem-core-1-0-phase-1-foundation.md`

**Deliverables:**

- Python 3.12-3.14 metadata and CI matrix; LF rules and repository artifact hygiene.
- Actual-byte ASGI request-body enforcement for declared, chunked, and headerless requests.
- Historical recall excludes current Policy and Derivation records.
- Explicit SQLite connection ownership and zero ResourceWarnings.
- 1.x compatibility policy scaffold and regression checks for the above contracts.

**Exit gate:** targeted tests pass, full suite passes with `-W error::ResourceWarning`, formatting/type/import checks pass, and no public schema changes occur except the deliberate 400/413 request behavior.

## Phase 2: Versioned configuration and upgrade experience

**Plan path:** `docs/superpowers/plans/2026-08-30-hl-mem-core-1-0-phase-2-config.md`

**Plan creation trigger:** Phase 1 is merged and its exact `Settings`, `Database`, and CI signatures are stable.

**Fixed deliverables:**

- `src/hl_mem/config/{models,loader,migrate,secrets}.py` and a thin `hl_mem.settings` facade.
- `schema_version = 1` configuration with Database, Extraction, Retrieval, Governance, Lifecycle, Integration, Observability, and Plugins sections.
- Service-neutral `hl-mem init`, read-only `hl-mem doctor`, and dry-run-first `hl-mem config migrate`.
- Removal of public `init --offline`, production Fake defaults, tag channel, pre-filter, and relation discovery `auto` mode.
- Explicit migration of old Query Expansion/Resurrection `auto` to `off`, and explicit relation discovery `auto` to `audit`.
- Backup-first upgrade check and documented snapshot restore rollback; no down migrations.

**Exit gate:** config snapshot, migration fixture, doctor, backup/restore, CLI, empty install, and v0.36.1 upgrade tests pass; old runtime aliases are rejected with a migration command.

## Phase 3: Governed Provider transport, usage ledger, and plugin API

**Plan path:** `docs/superpowers/plans/2026-08-30-hl-mem-core-1-0-phase-3-provider-governance.md`

**Plan creation trigger:** Phase 2 is merged so plugin configuration and secret ownership are fixed.

**Fixed deliverables:**

- Host-owned LLM, Embedding, and Reranker clients using neutral `ProviderRequest`/`ProviderResponse` adapters.
- `UsageGovernor` sidecar with atomic `reserve`, `settle`, and `release`, conservative unknown-result accounting, and reservation recovery.
- Four governed paths: LLM completion, actual embedding request batch, rerank request, and image description request.
- `hl_mem.providers` Entry Point group; typed Manifest, version negotiation, allowlist discovery, conflict fail-closed, Registry, and host proxies.
- Stable public plugin contracts for LLM/Embedding/Reranker and an experimental Image preview protected by host `ImageInputGuard`.
- Built-in Providers migrated one capability at a time with old/new equivalence tests.

**Exit gate:** concurrency and crash-recovery budget tests pass; no enabled model call is absent from the usage ledger; built-in Provider payload/error/retry/metrics equivalence passes; plugin conflicts fail before serving traffic.

## Phase 4: Automatic behavior and relation governance

**Plan path:** `docs/superpowers/plans/2026-08-30-hl-mem-core-1-0-phase-4-automation.md`

**Plan creation trigger:** Phase 3 is merged so every model task can use the same budget and audit boundary.

**Fixed deliverables:**

- `workers/maintenance.py` separates deterministic maintenance from model/semantic jobs.
- Separate switches for deterministic near-copy review and LLM dedup.
- Queue-time and handler-time gates for every disableable semantic task.
- Migration marks disabled pending jobs `dead` with `disabled_by_v1_migration` and pending resurrection tasks `abandoned`.
- LLM conflict consolidation stops daily scheduling and becomes audit-case-only; direct Claim dispute/supersede code is removed.
- Deterministic Observation construction remains enabled; automatic Mental Model generation is not added.
- Relation discovery supports only `off|audit`; official edges require deterministic, manual, or approved-Proposal provenance.

**Exit gate:** default worker maintenance makes zero model calls, disabled pre-upgrade jobs cannot execute after upgrade, conflict consolidation cannot mutate Claim status, and relation proposal concurrency/idempotency tests pass.

## Phase 5: Targeted architecture cleanup and evaluation separation

**Status:** Complete on the Phase 5 implementation branch; final merge follows the full quality and installed-wheel gates.

**Plan path:** `docs/superpowers/plans/2026-08-30-hl-mem-core-1-0-phase-5-architecture.md`

**Plan creation trigger:** Phase 4 is merged so the final behavior boundaries are known before moving code.

**Fixed deliverables:**

- `ingest/extraction/` separates Prompt/Schema, parsing, repair, post-processing, and the `LLMExtractor` facade without changing extraction outputs.
- `application/recall.py` keeps orchestration; enrichment/context assembly and delivery materialization move behind focused internal functions.
- `api/routes/` owns memory, recall, experience, and maintenance routes; `server.py` retains factory and middleware assembly.
- `worker.py` retains runtime/lease/heartbeat orchestration; maintenance composition lives in `workers/maintenance.py`.
- Stable `hl-mem eval` remains in `src/hl_mem/evaluation`; `v030_*` research machinery and matching scripts/tests move to top-level `benchmarks/archive` and stay out of the wheel.
- Complexity ratchet ceilings decrease for every refactored hotspot; no unrelated file moves.

**Exit gate:** characterization tests prove identical public outputs and Provider payloads; OpenAPI/MCP snapshots only contain approved 1.0 changes; wheel contains stable evaluation but no archived research modules.

## Phase 6: Release gates, security automation, benchmark, and RC

**Plan path:** `docs/superpowers/plans/2026-08-30-hl-mem-core-1-0-phase-6-release.md`

**Plan creation trigger:** Phase 5 is merged and package/module paths are final.

**Fixed deliverables:**

- Coverage floor 80%, required public recall fixture, no fixture-missing skip branch, PR fast gate, and full test gate.
- Python 3.12-3.14 test/build/migration matrix.
- `SECURITY.md`, Dependabot, CodeQL, `pip-audit`, SBOM, SHA-pinned Actions, support matrix, and release checklist.
- Newly frozen public Benchmark protocol and comparable v0.36.1/1.0 RC results; historical C-series thresholds are not reused.
- Install, empty DB, historical DB, repeated migration, backup restore, plugin conflict, streaming limit, and default-zero-model-call release evidence.
- `1.0.0rc1`, seven uninterrupted observation days, and `1.0.0` only after all gates pass.

**Exit gate:** a release checklist artifact links every gate result. Any P0/P1, data-semantic, migration, or stable-contract fix resets the seven-day RC clock.

## Phase-plan authoring rule

Before executing Phases 2-6, use `superpowers:writing-plans` to create the named phase plan against the merged previous phase. Each phase plan must include exact current file paths, interfaces, failing tests, expected failures, minimal implementations, targeted/full verification commands, and one commit per task. The fixed deliverables and exit gates above cannot be weakened by a later phase plan; changing them requires a design amendment approved by the user.

## Common verification commands

Run from the repository root with the checked-in environment:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/unit/ -q --tb=short
& '.\.venv\Scripts\python.exe' -m pytest tests/ -q --tb=short --cov=hl_mem --cov-report=term-missing --cov-fail-under=80
& '.\.venv\Scripts\python.exe' -W error::ResourceWarning -m pytest tests/ -q --tb=short
& '.\.venv\Scripts\python.exe' -m ruff check .
& '.\.venv\Scripts\python.exe' -m black --check .
& '.\.venv\Scripts\python.exe' -m isort --check-only .
& '.\.venv\Scripts\python.exe' -m mypy src/hl_mem/ --ignore-missing-imports
& '.\.venv\Scripts\python.exe' scripts/check_imports.py
& '.\.venv\Scripts\python.exe' scripts/check_complexity_budget.py --ratchet
& '.\.venv\Scripts\python.exe' scripts/check_openapi_snapshot.py
& '.\.venv\Scripts\python.exe' scripts/check_mcp_snapshot.py
& '.\.venv\Scripts\python.exe' -m build
```

Each phase runs only its relevant targeted commands during TDD, then the entire command set at the phase gate. A failing unrelated user change is reported and preserved; it is never reset or overwritten.
