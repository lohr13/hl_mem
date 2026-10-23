# HL-Mem 1.1 Implementation Roadmap

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship HL-Mem 1.1 with materially better exact-entity retrieval, useful read-only operations reporting, real built-in and external Provider evidence, and cleaner Recall/Extraction boundaries without weakening the 1.x contracts.

**Architecture:** Keep the modular monolith, SQLite authority, two-channel FTS/Dense retrieval, and governed Provider runtime. Deliver four ordered phases: first make runtime cost and health observable, then move existing entity constraints before channel limits, prove the public Provider API with an independent plugin while slimming Recall, and finally slim Extraction, remove dead experiments, and close the release gates.

**Tech Stack:** Python 3.12-3.14, SQLite WAL/FTS5, FastAPI, Pydantic, httpx, `importlib.metadata`, uv, pytest, Ruff, Black, isort, mypy, GitHub Actions.

## Global Constraints

- Development baseline is `3f50072`; approved design is `40e004d`; development branch is `develop/1.1` in `.worktrees/develop-1.1`.
- `main` remains the 1.0 release line until `v1.0.0` is tagged. Any 1.0 P0/P1 fix lands on `main` first and is merged into `develop/1.1` the same day.
- Before publishing 1.1, merge the final `v1.0.0` line into `develop/1.1`; never merge 1.1 feature commits into `main` before that tag.
- Preserve REST, MCP, configuration schema, import/export, backup, and the stable LLM/Embedding/Reranker Provider Plugin API throughout 1.x.
- Exact-entity recall adds zero LLM calls and no additional normal-query Embedding call. A high-confidence query replaces the original embedding input with one residual search query.
- Preserve FTS + Dense + RRF. Do not add a third candidate channel, permanent entity score, online weight learning, ANN/HNSW, Graph, or a new storage backend.
- SQLite remains authoritative. Do not add a main-database migration for 1.1; existing entity links and indexes are sufficient.
- Real Provider evidence uses disposable data only, never production Claims. One run is bounded to 10 LLM requests, 30 embedding items, 100 rerank documents, and CNY 20 estimated cost; the entire 1.1 evidence budget is CNY 50 unless the user grants fresh approval.
- The external DashScope reference plugin is an independent distribution and repository. Its PyPI publication, the HL-Mem RC publication, and GitHub Releases each require separate final authorization.
- Runtime reports and release artifacts must not contain prompts, queries, Claims, model responses, endpoints, credentials, plugin options, or production database content.
- Refactor by responsibility with characterization tests. Do not perform a full-tree rename, introduce a DI/plugin framework, or split code only to satisfy a line count.
- Retain 0.x retired-key migration recognition, v0.36.1 fixtures, migration snapshots, changelog history, archive material, Tag soft boost, Image preview, and relation-discovery Beta.
- The coverage floor remains 80%. Every changed behavior follows failing test -> observed failure -> minimal implementation -> targeted pass -> relevant regression -> commit.
- Each task that changes tracked files is one reviewable commit. Pure verification/publication tasks keep the reviewed commit immutable. Do not combine default changes, storage behavior, public contract changes, and structural moves in one commit.
- Preserve unrelated user files and never stage `docs/research/v028-plan-draft.md`, `hl_mem.toml.v0.bak`, `.coverage`, `Temp/`, or `nul`.

---

## Phase Dependency Graph

```text
Phase 1: Operations report and built-in Provider evidence
    -> Phase 2: Exact-entity retrieval before candidate limits
        -> Phase 3: External Provider proof and Recall refactor
            -> Phase 4: Extraction refactor, cleanup, and release
```

Phases are sequential because later evidence depends on the preceding stable interfaces. Tasks within a phase may only be parallelized when their plans declare disjoint files and no shared schema or contract.

## Design Traceability

| Approved requirement | Executable owner |
|---|---|
| Read-only cost/latency/failure/job/SQLite report | Phase 1 Tasks 1-3 |
| Optional host pricing and real built-in Provider evidence under hard budget | Phase 1 Tasks 4-6 |
| Residual query and entity scope before FTS/Dense limits | Phase 2 Tasks 2-4 |
| Twenty-four targeted cases, Core comparison, default enforce | Phase 2 Tasks 1, 5-6 |
| Independent DashScope Provider distribution and live proof | Phase 3 Tasks 1-4 |
| Recall query-planning and side-effect responsibility split | Phase 3 Tasks 5-6 |
| Extraction orchestration/verification split | Phase 4 Tasks 1-3 |
| PostgreSQL/pre-filter/Tag-channel current-surface cleanup | Phase 4 Task 4 |
| Version, supply-chain, artifacts, 48-hour RC | Phase 4 Tasks 5-7 |

Every approved scope item has one owner. Graph, ANN, new databases, large paid evaluation sets, new LLM entity calls, plugin-market features, Settings/Hermes/Repository rewrites, and expensive automation remain explicit non-goals under Global Constraints.

## Phase 1: Operations Report and Built-in Provider Evidence

**Executable plan:** `docs/superpowers/plans/2026-08-31-hl-mem-1-1-phase-1-observability-provider.md`

**Deliverables:**

- Versioned, read-only `hl-mem ops report --since 24h|7d [--json]` output.
- Existing usage-ledger aggregation by capability/provider/model/status, including usage units, cost, failures, reservations, and P50/P95.
- Optional versioned CNY price book so the host can reserve and settle estimated Provider cost without exposing pricing to plugins.
- Job, worker-observation, SQLite-file, conflict-backlog, and recall-side-effect summaries without migrations or repair side effects.
- Cheap `/healthz` additions for current-day failures, stale reservations, and budget utilization only.
- An explicit `benchmarks/provider/` live smoke with disposable state, hard budgets, redacted artifacts, and built-in LLM/Embedding/Reranker evidence.

**Exit gate:** empty/normal/failure/corrupt report cases are deterministic; the CLI never writes either database; health remains cheap; a built-in real-Provider run succeeds within budget and leaves zero active reservations.

## Phase 2: Exact-Entity Retrieval Before Candidate Limits

**Executable plan:** `docs/superpowers/plans/2026-08-31-hl-mem-1-1-phase-2-entity-recall.md`

**Deliverables:**

- Safe normalized mention spans, residual query, search query, and explicit `entity|observe|wide|off` scope plan.
- Entity scope in FTS SQL and local Dense scan before `LIMIT`; normal sqlite-vec and wide-query behavior remain unchanged.
- Wide fallback on ambiguity, incomplete links, historical alias, and entity-planning/read failures.
- Twenty-four deterministic entity regression cases plus the frozen Core 1.0 comparator.
- Beta default change from `observe` to `enforce`, with explicit user `off|observe` values preserved.

**Exit gate:** every high-confidence expected Claim enters Top 5, cross-entity Top 1 is zero, wide-fallback cases match 1.0, Core 1.0 metrics regress by no more than 0.01, entity P95 stays within the approved bound, and model-call counts do not increase.

## Phase 3: External Provider Proof and Recall Refactor

**Executable plan:** `docs/superpowers/plans/2026-08-31-hl-mem-1-1-phase-3-plugin-recall.md`

**Deliverables:**

- Independent `hl-mem-provider-dashscope` source repository and versioned wheel implementing stable LLM, Embedding, and Reranker adapters using only `hl_mem.plugins`.
- Clean-environment discovery, allowlist, compatibility, conflict, error-isolation, governance, and real-service evidence.
- `recall/query_planning.py` owns query-expansion/entity/search-query planning without changing public Recall behavior.
- `application/recall_side_effects.py` owns access/exposure retry and failure audit; `RecallService` remains the public facade and preserves documented patch points.
- Complexity ceilings ratcheted down to measured post-refactor values.

**Exit gate:** the independent wheel passes contract and live smoke gates, all calls appear in the host usage ledger, built-ins remain available, Recall characterization is unchanged, and `application/recall.py` has a lower complexity ceiling.

## Phase 4: Extraction Refactor, Cleanup, and Release

**Executable plan:** `docs/superpowers/plans/2026-08-31-hl-mem-1-1-phase-4-extraction-release.md`

**Deliverables:**

- `ingest/extraction/orchestrator.py` owns chunk/split/repair/retry/merge state; `verification.py` owns entailment verification, accounting, and failure audit.
- `LLMExtractor` remains the compatible facade with identical prompts, schemas, Provider calls, idempotency, transactions, and patch points.
- PostgreSQL probe code/test removed; current docs stop advertising PostgreSQL probe, extraction pre-filter, and independent Tag channel.
- Final contracts, security scans, builds, install checks, migration/restore checks, benchmarks, and sanitized evidence manifest.
- `1.1.0rc1` package prepared; publication remains authorization-gated; 48-hour RC observation before stable `1.1.0`.

**Exit gate:** Extraction characterization is unchanged, complexity ceiling is lower, all repository gates pass on Python 3.12-3.14, the wheel contains no live data/plugin source/research cache, and RC evidence has no unresolved P0/P1.

## Common Verification Commands

Run from the active 1.1 worktree:

```powershell
uv run --frozen python -m pytest tests/unit/ -q --tb=short
uv run --frozen python -m pytest tests/ -q --tb=short --cov=hl_mem --cov-report=term-missing --cov-fail-under=80
uv run --frozen python -W error::ResourceWarning -m pytest tests/ -q --tb=short
uv run --frozen python -m ruff check .
uv run --frozen python -m black --check .
uv run --frozen python -m isort --check-only .
uv run --frozen python -m mypy src/hl_mem/ --ignore-missing-imports
uv run --frozen python scripts/check_imports.py
uv run --frozen python scripts/check_complexity_budget.py --ratchet
uv run --frozen python scripts/check_config_schema_snapshot.py
uv run --frozen python scripts/check_openapi_snapshot.py
uv run --frozen python scripts/check_mcp_snapshot.py
uv run --frozen python scripts/check_provider_plugin_api.py
uv run --frozen python -m build
```

Each task runs its focused tests first. Each phase runs the complete relevant gate set, records the exact commit and outputs, and stops on unexplained regression rather than weakening thresholds.
