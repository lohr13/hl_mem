# HL-Mem Core 1.0 Phase 4 Automation and Relation Governance Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task by task. Use TDD for every behavior change and run the stated verification after each task.

**Goal:** Make background automation explicit and safe, ensure disabled semantic jobs cannot execute after upgrade, convert LLM conflict consolidation to audit-only behavior, and require provenance for all new official memory relations.

**Architecture:** Keep deterministic maintenance enabled and free of model calls. Put maintenance assembly in one focused module, use one typed policy function for disableable semantic jobs, and enforce that policy at scheduling and handler boundaries. Persist upgrade cleanup and relation provenance in migrations. Do not introduce a generic workflow engine, plugin hook, approval API, or graph database.

**Tech Stack:** Python 3.12-3.14, dataclasses, SQLite migrations, pytest, Ruff, Black, isort, mypy.

---

## Fixed behavior

Default-on work:

- TTL, expiry, decay, archive, cleanup, stale propagation and dangling repair.
- Deterministic Observation construction.
- Deterministic near-copy review.
- L0 deterministic conflict resolution.
- Plan fulfillment and explicit extraction/access/feedback tasks.

Default-off work:

- LLM conflict consolidation.
- LLM deduplication.
- Automatic Policy induction/publication.
- LLM reclassification.
- Query expansion, resurrection and relation discovery.

Additional invariants:

- `dedup.enabled` remains the compatible public switch for deterministic near-copy review.
- `dedup.llm_enabled` is the new, independent paid-dedup switch and defaults to false.
- A disabled semantic job is rejected before enqueue and again before handler construction.
- Upgrade cleanup marks old pending semantic jobs `dead` and pending resurrection deferred tasks `abandoned`.
- Conflict consolidation records judgments but never changes Claim status, supersession, memory relations, or conflict cases.
- New official memory relations require provenance: `deterministic`, `manual`, or `approved_proposal`.
- Historical relations retain `legacy` provenance; new write APIs cannot create `legacy` rows.

## Task 1: Add explicit automation policy and configuration

**Files:**

- Modify: `src/hl_mem/config/lifecycle.py`
- Modify: `src/hl_mem/config/models.py`
- Modify: `src/hl_mem/config/migrate.py`
- Modify: `config.example.toml`
- Regenerate: `docs/configuration.md`
- Regenerate: `docs/config-schema.json`
- Create: `src/hl_mem/workers/automation.py`
- Modify: `tests/unit/test_config_loader.py`
- Modify: `tests/unit/test_config_migrate.py`
- Create: `tests/unit/test_worker_automation.py`
- Modify: `tests/fixtures/config/v1-online.toml`

**Step 1: Write failing tests**

Cover these defaults and mappings:

- `dedup.enabled=true` controls only deterministic near-copy review.
- `dedup.llm_enabled=false` by default.
- `worker.semantic_conflict_consolidation_enabled=false` by default.
- `worker.policy_induction_enabled=false` by default.
- `worker.reclassify_enabled=false` by default.
- `semantic_job_enabled(settings, job_type)` maps all five semantic job types and rejects unknown types.
- v0.36.1 config migration emits the new safe defaults without changing the deterministic dedup setting.

Run:

```powershell
uv run --frozen --extra sqlite-vec python -m pytest tests/unit/test_config_loader.py tests/unit/test_config_migrate.py tests/unit/test_worker_automation.py -q
```

Expected: failure because fields and policy do not exist.

**Step 2: Implement the smallest typed policy**

- Add the four new booleans.
- Keep `dedup.enabled` and its external meaning as deterministic near-copy review.
- Define `SemanticJobType` and a total mapping in `workers/automation.py`.
- Do not make the policy module enqueue jobs or access SQLite.
- Update config migration and generated example.
- Regenerate the checked-in configuration reference and schema with the repository script.

**Step 3: Verify**

Run the command from Step 1, then:

```powershell
uv run --frozen ruff check src/hl_mem/config src/hl_mem/workers/automation.py tests/unit/test_worker_automation.py
uv run --frozen mypy src/hl_mem
```

**Step 4: Commit**

```powershell
git add src/hl_mem/config src/hl_mem/workers/automation.py tests/unit/test_config_loader.py tests/unit/test_config_migrate.py tests/unit/test_worker_automation.py tests/fixtures/config/v1-online.toml
git commit -m "feat: make semantic automation explicit"
```

## Task 2: Separate deterministic maintenance from semantic scheduling

**Files:**

- Create: `src/hl_mem/workers/maintenance.py`
- Modify: `src/hl_mem/workers/worker.py`
- Modify: `tests/unit/test_worker.py`
- Modify: `tests/unit/test_daily_memory_api.py`
- Create: `tests/unit/test_worker_maintenance.py`

**Step 1: Write failing tests**

Prove that:

- Default maintenance assembles no job that can call a model.
- Near-copy review remains present by default and disappears only when `dedup.enabled=false`.
- Every semantic daily job appears only when its own switch is true.
- Reclassification is not queued merely because a model key exists.
- Policy induction is not queued by default even though its implementation is deterministic.
- Maintenance operations execute in the existing order and preserve failure isolation.

Run:

```powershell
uv run --frozen --extra sqlite-vec python -m pytest tests/unit/test_worker.py tests/unit/test_daily_memory_api.py tests/unit/test_worker_maintenance.py -q
```

Expected: failures expose the current mixed maintenance list and unsafe defaults.

**Step 2: Extract a focused maintenance module**

- Move list construction, not the Worker lifecycle, into `workers/maintenance.py`.
- Use a small immutable `MaintenanceOperation` value object with a name and callable.
- Provide separate builders for deterministic operations and semantic enqueue operations.
- Keep model-provider construction outside deterministic builders.
- Keep `Worker._run_maintenance()` as orchestration and logging only.

**Step 3: Verify and commit**

Run the tests from Step 1, then:

```powershell
uv run --frozen ruff check src/hl_mem/workers tests/unit/test_worker.py tests/unit/test_worker_maintenance.py
uv run --frozen black --check src/hl_mem/workers tests/unit/test_worker.py tests/unit/test_worker_maintenance.py
uv run --frozen isort --check-only src/hl_mem/workers tests/unit/test_worker.py tests/unit/test_worker_maintenance.py
```

Commit:

```powershell
git add src/hl_mem/workers/worker.py src/hl_mem/workers/maintenance.py tests/unit/test_worker.py tests/unit/test_daily_memory_api.py tests/unit/test_worker_maintenance.py
git commit -m "refactor: separate maintenance automation"
```

## Task 3: Enforce queue-time and handler-time gates

**Files:**

- Modify: `src/hl_mem/workers/job_handlers.py`
- Modify: `src/hl_mem/workers/consolidate.py`
- Modify: `src/hl_mem/workers/deduplicate.py`
- Modify: `src/hl_mem/workers/induce_policies.py`
- Modify: `src/hl_mem/workers/reclassify.py`
- Modify: `src/hl_mem/application/ingest.py`
- Modify: `src/hl_mem/api/server.py`
- Modify: `src/hl_mem/workers/deferred.py`
- Modify: `src/hl_mem/workers/worker.py`
- Modify: `tests/unit/test_consolidation_scope.py`
- Modify: `tests/unit/test_relation_discovery.py`
- Modify: `tests/unit/test_worker.py`
- Create: `tests/unit/test_semantic_job_gates.py`
- Create: `tests/unit/test_resurrection_handler_gate.py`

**Step 1: Write failing tests**

For every semantic job type, assert:

- disabled queue helpers do not insert jobs;
- disabled manually inserted jobs return a stable `disabled_by_configuration` result;
- no provider factory or model client is constructed in the disabled path;
- enabling only one semantic capability does not enable another;
- `/v1/consolidate` rejects a disabled capability without inserting a job;
- relation discovery remains `off|audit`, with `off` blocking queue and handler execution;
- a pre-existing due resurrection task is abandoned when resurrection is off.

Run:

```powershell
uv run --frozen --extra sqlite-vec python -m pytest tests/unit/test_semantic_job_gates.py tests/unit/test_resurrection_handler_gate.py tests/unit/test_consolidation_scope.py tests/unit/test_relation_discovery.py tests/unit/test_worker.py -q
```

Expected: handler and deferred-task assertions fail.

**Step 2: Implement both gates**

- Reuse `semantic_job_enabled`; do not duplicate switch logic.
- Add explicit `enabled` arguments to scheduling helpers.
- Check disabled state in `dispatch_job()` before resolving the handler.
- Return a stable disabled result so the Worker can terminate the stale job without retry.
- Pass a disabled deferred-task type set into deferred processing and abandon resurrection before invoking its handler.
- Preserve explicit extraction and recall-side-effect behavior.

**Step 3: Verify and commit**

Run the tests from Step 1 plus:

```powershell
uv run --frozen --extra sqlite-vec python -m pytest tests/unit/test_daily_memory_api.py tests/unit/test_ingest_transaction_characterization_v0293.py tests/unit/test_relation_discovery.py tests/unit/test_worker.py -q
uv run --frozen mypy src/hl_mem
```

Commit:

```powershell
git add src/hl_mem/workers src/hl_mem/application/ingest.py src/hl_mem/api/server.py tests/unit
git commit -m "feat: gate semantic jobs at enqueue and execution"
```

## Task 4: Neutralize pre-upgrade semantic work

**Files:**

- Create: `src/hl_mem/storage/migrations/058_disable_v1_semantic_jobs.sql`
- Create: `tests/unit/test_migration_058_disable_v1_semantic_jobs.py`
- Modify: migration snapshot/manifest files discovered by the migration harness

**Step 1: Write a failing migration test**

Seed pending, running and terminal jobs for all semantic and unrelated job types, plus pending resurrection and unrelated deferred tasks. Assert after migration:

- pending semantic jobs are `dead`, leases cleared, `last_error='disabled_by_v1_migration'`;
- running and terminal jobs are unchanged;
- pending resurrection is `abandoned` with the same reason;
- unrelated jobs and deferred tasks are unchanged;
- applying the migration twice has the same result.

Run:

```powershell
uv run --frozen --extra sqlite-vec python -m pytest tests/unit/test_migration_058_disable_v1_semantic_jobs.py -q
```

Expected: fail because migration 058 is absent.

**Step 2: Add the bounded migration**

- Update only `status='pending'` rows.
- Include `consolidate_conflicts`, `deduplicate_claims`, `discover_relations`, `induce_policies`, and `reclassify_claims`.
- Do not cancel extraction, deterministic maintenance, or user-triggered recall side effects.

**Step 3: Verify and commit**

```powershell
uv run --frozen --extra sqlite-vec python -m pytest tests/unit/test_migration_058_disable_v1_semantic_jobs.py tests/unit/test_migration_057_retire_conflict_l2_jobs.py -q
git add src/hl_mem/storage/migrations tests/unit/test_migration_058_disable_v1_semantic_jobs.py
git commit -m "fix: neutralize legacy semantic jobs on upgrade"
```

## Task 5: Make semantic conflict consolidation audit-only

**Files:**

- Modify: `src/hl_mem/workers/consolidate.py`
- Modify: `tests/unit/test_consolidate.py`
- Modify: `tests/unit/test_consolidation_scope.py`
- Create: `tests/unit/test_consolidation_audit_only.py`

**Step 1: Write failing immutability tests**

For high-confidence contradiction and state-change decisions, snapshot and compare:

- Claim rows and statuses;
- `memory_relations`;
- `conflict_cases`;
- supersession fields;
- relevant evidence links.

Assert only `consolidation_pairs` and model-usage/audit records may change. Assert dry-run writes nothing.

Run:

```powershell
uv run --frozen --extra sqlite-vec python -m pytest tests/unit/test_consolidate.py tests/unit/test_consolidation_scope.py tests/unit/test_consolidation_audit_only.py -q
```

Expected: contradiction and state-change tests fail because the current implementation mutates Claims.

**Step 2: Remove mutation branches**

- Keep candidate scan, model judgment, confidence classification, CAS freshness check and audit recording.
- Record every accepted judgment as `audit_only:<kind>` so audit mode and model classification are both preserved.
- Remove direct Claim disputes, conflict-case creation, and supersession.
- Do not create a replacement auto-apply path.
- Keep the explicit human/delegation conflict workflow untouched.

**Step 3: Verify and commit**

```powershell
uv run --frozen --extra sqlite-vec python -m pytest tests/unit/test_consolidate.py tests/unit/test_consolidation_scope.py tests/unit/test_consolidation_audit_only.py tests/unit/test_conflict_snapshot.py -q
uv run --frozen ruff check src/hl_mem/workers/consolidate.py tests/unit/test_consolidation_audit_only.py
git add src/hl_mem/workers/consolidate.py tests/unit/test_consolidate.py tests/unit/test_consolidation_scope.py tests/unit/test_consolidation_audit_only.py
git commit -m "fix: make semantic conflict review audit only"
```

## Task 6: Require provenance for official memory relations

**Files:**

- Create: `src/hl_mem/storage/migrations/059_memory_relation_provenance.sql`
- Modify: `src/hl_mem/domain/relations.py`
- Modify: `src/hl_mem/storage/relation_proposals.py`
- Modify: `src/hl_mem/storage/plan_fulfillments.py`
- Modify: `tests/unit/test_relation_expansion.py`
- Modify: `tests/unit/test_relation_discovery.py`
- Modify: `tests/unit/test_p0_3_relation_concurrent.py`
- Modify: `tests/unit/test_p1_3_relation_proposal_runs.py`
- Create: `tests/unit/test_migration_059_memory_relation_provenance.py`
- Create: `tests/unit/test_relation_provenance.py`

**Step 1: Write failing tests**

Assert:

- migration adds `provenance` and nullable `proposal_id` while preserving existing rows as `legacy`;
- new relation writes require explicit `deterministic`, `manual`, or `approved_proposal` provenance;
- approving a pending Proposal atomically creates a matching `approved_proposal` relation and marks the Proposal `applied`;
- deterministic Plan fulfillment writes `deterministic` provenance;
- no new API can write `legacy` provenance;
- concurrent audit relation discovery stays proposal-only and idempotent;
- relation expansion behavior is unchanged.

Run:

```powershell
uv run --frozen --extra sqlite-vec python -m pytest tests/unit/test_migration_059_memory_relation_provenance.py tests/unit/test_relation_provenance.py tests/unit/test_relation_discovery.py tests/unit/test_p0_3_relation_concurrent.py tests/unit/test_p1_3_relation_proposal_runs.py -q
```

Expected: fail because the columns and required provenance are absent.

**Step 2: Implement provenance without a new public approval API**

- Add the two columns and indexes needed for proposal lookup.
- Introduce `RelationProvenance` in the domain module.
- Require keyword-only provenance for all new official relation writes.
- Add one internal repository approval operation that validates endpoints and relation type, inserts the relation, and marks the Proposal `applied` in the same transaction.
- Keep `evidence_json` as evidence IDs; do not overload it with provenance metadata.
- Update Plan fulfillment to use the shared domain write path instead of duplicate SQL.

**Step 3: Verify and commit**

```powershell
uv run --frozen --extra sqlite-vec python -m pytest tests/unit/test_relation_expansion.py tests/unit/test_relation_discovery.py tests/unit/test_p0_3_relation_concurrent.py tests/unit/test_p1_3_relation_proposal_runs.py tests/unit/test_migration_059_memory_relation_provenance.py tests/unit/test_relation_provenance.py -q
uv run --frozen mypy src/hl_mem
git add src/hl_mem/storage/migrations src/hl_mem/domain/relations.py src/hl_mem/storage/relation_proposals.py src/hl_mem/storage/plan_fulfillments.py tests/unit
git commit -m "feat: require memory relation provenance"
```

## Task 7: Close Phase 4 contracts and verification

**Files:**

- Modify: `docs/config.md`
- Modify: `docs/capability-matrix.md`
- Modify: `docs/operations.md`
- Modify: `docs/compatibility.md`
- Modify: relevant OpenAPI/MCP/config snapshots only when generated behavior changed

**Step 1: Document only shipped behavior**

- List every automatic task and its default.
- Explain deterministic near-copy versus paid LLM dedup.
- Document disabled-job upgrade behavior.
- State that semantic consolidation is audit-only.
- State relation provenance values and the grandfathered `legacy` read state.
- Do not promise a relation approval API, GraphStore, or graph database.

**Step 2: Run focused phase tests**

```powershell
uv run --frozen --extra sqlite-vec python -m pytest tests/unit/test_worker_automation.py tests/unit/test_worker_maintenance.py tests/unit/test_semantic_job_gates.py tests/unit/test_resurrection_handler_gate.py tests/unit/test_migration_058_disable_v1_semantic_jobs.py tests/unit/test_consolidation_audit_only.py tests/unit/test_migration_059_memory_relation_provenance.py tests/unit/test_relation_provenance.py -q
```

**Step 3: Run all project gates**

```powershell
uv run --frozen ruff check .
uv run --frozen black --check .
uv run --frozen isort --check-only .
uv run --frozen mypy src/hl_mem
uv run --frozen python scripts/check_complexity_budget.py --ratchet
uv run --frozen python scripts/check_imports.py
uv run --frozen python scripts/check_config_schema_snapshot.py
uv run --frozen python scripts/check_provider_plugin_api.py
uv run --frozen python scripts/check_openapi_snapshot.py
uv run --frozen python scripts/check_mcp_snapshot.py
uv run --frozen python scripts/check_docs_consistency.py
uv run --frozen --extra sqlite-vec python -m pytest -q
```

Expected: every command passes, no `ResourceWarning`, and the full suite reports zero failures.

**Step 4: Build and install smoke**

```powershell
uv run --frozen python -m build
```

Install the wheel into a fresh temporary virtual environment and run:

```powershell
hl-mem --help
hl-mem doctor
```

**Step 5: Review the diff and commit docs**

```powershell
git diff --check
git status --short
git add docs/config.md docs/capability-matrix.md docs/operations.md docs/compatibility.md
git commit -m "docs: define automation and relation governance"
```

## Phase 4 completion criteria

- Default Worker maintenance performs zero model calls.
- Every disableable semantic task has queue-time, handler-time and upgrade-time protection.
- Pending pre-upgrade resurrection work cannot reactivate memory after upgrade.
- Semantic conflict consolidation cannot mutate Claims or conflict state.
- Relation discovery remains proposal-only and official relation writes have explicit provenance.
- All focused, contract, migration, quality, full-suite, build and fresh-install checks pass.
- Work is locally merged to `main`; nothing is pushed remotely.
