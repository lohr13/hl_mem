# HL-Mem Core 1.0 Phase 2 Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a versioned Core 1.0 configuration boundary, deterministic 0.x migration, service-neutral initialization, and a read-only diagnostic path without retaining production Fake fallbacks or retired configuration surfaces.

**Architecture:** Split the current monolithic settings implementation into eight typed dataclass groups and combine them into one immutable, flat runtime `Settings` snapshot so existing application reads stay stable without duplicating 209 fields. Move loading, secrets, and migration into `hl_mem.config`; require `schema_version = 1` at every production entry point; keep `hl_mem.settings` and `hl_mem.config_loader` as thin internal import facades. Use `tomli-w==1.2.0` only for deterministic TOML emission, then parse and validate every emitted document with `tomllib` before atomic replacement.

**Tech Stack:** Python 3.12-3.14, dataclasses, `tomllib`, `tomli-w==1.2.0`, argparse, SQLite, httpx, pytest, uv.

## Global Constraints

- Base commit is Phase 1 `e64037f`; implementation branch is `codex/core-1-0-phase-2` in `.worktrees/core-1-0-phase-2`.
- Follow test-driven development for every behavior change: add one failing test, observe the intended failure, implement the minimum behavior, then rerun the focused suite.
- The v1 runtime accepts only `schema_version = 1`; an absent or future version fails with an explicit `hl-mem config migrate` instruction.
- Keep existing TOML key paths for capabilities that remain supported. Python grouping must not force users to learn a second naming scheme.
- The runtime `Settings` snapshot remains immutable and flat for consumers. Grouping is implemented by typed dataclass bases with disjoint field ownership and group-local validation, not duplicated nested and flat models.
- `Settings.for_test()` remains the only Fake configuration factory. Configurations loaded from TOML must explicitly select model-backed extraction and embedding and must never activate a Fake implementation.
- Secrets come only from the supported process environment or `.env`; they never appear in TOML, migration reports, exceptions, snapshots, logs, Trace, or Audit.
- `hl-mem doctor` may read files, open SQLite read-only, create a disposable copy in the system temporary directory, and make explicit provider health requests. It must not modify the configured TOML, `.env`, production database, tombstone ledger, or installed plugin.
- Config migration is dry-run by default. `--apply` backs up the original config, requires a verified recovery set when the configured database exists, validates the emitted TOML, and uses same-directory atomic replacement.
- Keep SQLite authoritative and migrations forward-only. This phase adds no down migration and no new memory table.
- Do not start Provider Plugin API, usage-ledger, worker automation, or broad architecture cleanup work assigned to Phases 3-5.
- Preserve the user's unrelated untracked files and every worktree outside `.worktrees/core-1-0-phase-2`.

---

## Task 1: Split configuration ownership without changing runtime reads

**Files:**

- Create: `src/hl_mem/config/__init__.py`
- Delete: `src/hl_mem/config.py` after moving its public constants to `src/hl_mem/config/__init__.py`
- Create: `src/hl_mem/config/models.py`
- Create: `src/hl_mem/config/secrets.py`
- Create: `src/hl_mem/config/loader.py`
- Modify: `src/hl_mem/settings.py`
- Modify: `src/hl_mem/config_loader.py`
- Create: `tests/unit/test_config_module_boundaries.py`
- Modify: `tests/unit/test_config_loader.py`
- Modify: `tests/unit/test_settings_contract.py`

**Interfaces:**

- `CONFIG_SCHEMA_VERSION: Final[int] = 1` is defined in `hl_mem.config.models`; the `schema_version` field and enforcement are added in Task 2 so Task 1 remains behavior-preserving.
- Eight frozen dataclass bases own disjoint flat fields: `DatabaseConfig`, `ExtractionConfig`, `RetrievalConfig`, `GovernanceConfig`, `LifecycleConfig`, `IntegrationConfig`, `ObservabilityConfig`, and `PluginsConfig`.
- `Settings` inherits those bases and owns only cross-group methods: `validate()`, `validate_runtime()`, `for_test()`, `snapshot()`, `retention_policy()`, and `query_expansion_line_overrides()`.
- `iter_config_fields()` returns all TOML and secret-backed dataclass fields exactly once, independent of module location or inheritance order.
- `load_settings(config_path=None, env_path=None, *, environ=None, validate_runtime=True) -> Settings` is implemented in `hl_mem.config.loader`.
- `read_secret_values(path, names, environ) -> dict[str, str]`, `is_placeholder_secret(value) -> bool`, and `redact_secret_text(text, values) -> str` are implemented in `hl_mem.config.secrets`.
- `[plugins].enabled` is parsed as an ordered, duplicate-free allowlist. Arbitrary TOML-native values under `[plugins.<id>]` are captured as an immutable per-plugin mapping without importing or executing plugin code. Plugin IDs must match `[a-z0-9][a-z0-9._-]{0,63}`; malformed IDs and a table whose ID collides with the reserved `enabled` key fail closed.
- `hl_mem.settings` re-exports model types/helpers; `hl_mem.config_loader` re-exports loader functions. Neither facade contains field definitions, parsing, or validation logic.

- [ ] **Step 1: Add a failing module-boundary test**

```python
def test_legacy_imports_are_thin_identity_facades() -> None:
    from hl_mem.config.loader import load_settings as canonical_loader
    from hl_mem.config.models import Settings as canonical_settings
    from hl_mem.config_loader import load_settings
    from hl_mem.settings import Settings

    assert Settings is canonical_settings
    assert load_settings is canonical_loader
```

Also assert every field appears in exactly one group and every field declares one supported source: `toml`, `secret_env`, or the root-only `schema_version` marker.

- [ ] **Step 2: Run the new test and observe the missing-package failure**

Run:

```powershell
uv run --frozen python -m pytest tests/unit/test_config_module_boundaries.py -q --tb=short
```

Expected: collection fails because `hl_mem.config.loader` and `hl_mem.config.models` do not exist.

- [ ] **Step 3: Move fields and validation into typed groups**

Assign existing fields by TOML prefix:

```python
GROUP_PREFIXES = {
    DatabaseConfig: {"database", "index"},
    ExtractionConfig: {"llm", "embedding", "reranker", "image_describer", "extraction"},
    RetrievalConfig: {"recall"},
    GovernanceConfig: {"entity", "dedup", "conflict", "relation", "price", "plan", "state"},
    LifecycleConfig: {"retention", "decay", "worker"},
    IntegrationConfig: {"server", "hermes"},
    ObservabilityConfig: {"monitoring"},
    PluginsConfig: {"plugins"},
}
```

Each base exposes a uniquely named validation method such as `_validate_database()`; `Settings.validate()` calls all eight explicitly so MRO does not choose validation behavior. Preserve existing field names and TOML metadata byte-for-byte in this task.

- [ ] **Step 4: Move secret and loader logic and reduce facades**

Move `_read_env_file()` and placeholder recognition to `secrets.py`; move `_resolve_database_path()`, type coercion, schema construction, flattening, and `load_settings()` to `loader.py`. Replace the old modules with explicit imports and `__all__`; do not use wildcard imports.

- [ ] **Step 5: Run focused configuration tests**

```powershell
uv run --frozen python -m pytest tests/unit/test_config_module_boundaries.py tests/unit/test_config_loader.py tests/unit/test_settings_contract.py tests/unit/test_config_assembly.py -q --tb=short
uv run --frozen python -m mypy src/hl_mem/config src/hl_mem/settings.py src/hl_mem/config_loader.py --ignore-missing-imports
```

Expected: all pass; `Settings` still has the same fields and values as the Phase 1 baseline.

- [ ] **Step 6: Commit the structural split**

```powershell
git add src/hl_mem/config src/hl_mem/settings.py src/hl_mem/config_loader.py tests/unit/test_config_module_boundaries.py tests/unit/test_config_loader.py tests/unit/test_settings_contract.py
git commit -m "refactor: split typed configuration ownership"
```

---

## Task 2: Enforce the v1 schema and production runtime profile

**Files:**

- Modify: `src/hl_mem/config/models.py`
- Modify: `src/hl_mem/config/loader.py`
- Modify: `src/hl_mem/config/secrets.py`
- Modify: `config.example.toml`
- Modify: `tests/unit/test_config_loader.py`
- Modify: `tests/unit/test_settings_contract.py`
- Modify: `tests/unit/test_p1_8_settings_modes.py`
- Modify: `tests/unit/test_doctor.py`
- Modify: any focused test fixture that calls `load_settings()` with an unversioned inline TOML document

**Interfaces:**

- `load_settings(..., validate_runtime=True)` rejects missing/future schema versions and configurations that select Fake extraction/embedding or lack enabled providers' keys.
- `load_settings(..., validate_runtime=False)` still requires schema version and valid types/modes but leaves missing-key reporting to `doctor`.
- `Settings.validate()` checks structural and cross-field invariants without network access.
- `Settings.validate_runtime()` additionally rejects `extraction.mode = "fake"`, `embedding.mode = "fake"`, placeholder/missing enabled secrets, and incomplete provider/model/base-URL selections.
- `Settings.for_test()` returns Fake extraction/embedding, disables all optional network paths, and never passes through the TOML loader.
- Plain `Settings()` defaults to model-backed extraction/embedding; focused tests that require Fakes must migrate to `Settings.for_test()` or explicit injected doubles. Do not preserve a second implicit Fake path for test convenience.

- [ ] **Step 1: Write failing schema-version tests**

```python
def test_unversioned_config_requires_migration(tmp_path: Path) -> None:
    path = _write(tmp_path / "legacy.toml", "[database]\npath='memory.db'\n")
    with pytest.raises(ConfigurationError, match=r"schema_version.*hl-mem config migrate"):
        load_settings(path, environ={})


def test_future_config_fails_without_guessing(tmp_path: Path) -> None:
    path = _write(tmp_path / "future.toml", "schema_version = 2\n")
    with pytest.raises(ConfigurationError, match=r"unsupported schema_version 2"):
        load_settings(path, environ={})
```

Add a v1 production fixture with `extraction.mode = "llm"`, `embedding.mode = "real"`, explicit provider/model/base URLs, and opaque non-placeholder keys; assert it loads. Add tests that v1 Fake modes fail even when keys exist and that `Settings.for_test()` remains valid.

- [ ] **Step 2: Run the schema-version tests and observe current permissive loading**

```powershell
uv run --frozen python -m pytest tests/unit/test_config_loader.py -q --tb=short
```

Expected: the new legacy/future/Fake tests fail because Phase 1 accepts unversioned files and Fake runtime modes.

- [ ] **Step 3: Implement root version parsing and runtime validation**

Recognize `schema_version` before flattening. Require `type(value) is int` and `value == CONFIG_SCHEMA_VERSION`; exclude it from ordinary field traversal. Error messages include the resolved config path and migration command but never environment values.

Change static dataclass defaults to model-backed extraction/embedding and require production TOML to explicitly contain both modes; omitted keys must not select or inherit Fake values. Convert only affected tests to `Settings.for_test()` or explicit injected doubles rather than mechanically rewriting every constructor. Keep query expansion, resurrection, relation discovery, image description, and reranking off in the canonical v1 example.

- [ ] **Step 4: Convert configuration tests to explicit v1 fixtures**

Add a local helper:

```python
def _v1(body: str = "") -> str:
    return "schema_version = 1\n" + body
```

Use it only where the test is exercising the v1 loader. Keep intentionally unversioned documents in migration/rejection tests. Use `validate_runtime=False` only in doctor diagnostics and tests explicitly checking structural parsing.

- [ ] **Step 5: Update the canonical example and run focused tests**

Place `schema_version = 1` before all tables in `config.example.toml`; explicitly select model-backed extraction/embedding; set `recall.query_expansion_mode = "off"`, `recall.resurrection_mode = "off"`, and `relation.discovery_mode = "off"`.

```powershell
uv run --frozen python -m pytest tests/unit/test_config_loader.py tests/unit/test_settings_contract.py tests/unit/test_p1_8_settings_modes.py tests/unit/test_doctor.py -q --tb=short
```

- [ ] **Step 6: Commit the v1 runtime boundary**

```powershell
git add src/hl_mem/config config.example.toml tests/unit/test_config_loader.py tests/unit/test_settings_contract.py tests/unit/test_p1_8_settings_modes.py tests/unit/test_doctor.py
git commit -m "feat: enforce versioned production configuration"
```

---

## Task 3: Remove retired surfaces and unsafe automatic relation application

**Files:**

- Modify: `src/hl_mem/config/models.py`
- Modify: `src/hl_mem/application/recall.py`
- Modify: `src/hl_mem/recall/staged_pipeline.py`
- Modify: `src/hl_mem/recall/trace.py`
- Modify: `src/hl_mem/workers/worker.py`
- Modify: `src/hl_mem/workers/job_handlers.py`
- Modify: `src/hl_mem/workers/discover_relations.py`
- Delete: `src/hl_mem/ingest/pre_filter.py`
- Modify: `tests/unit/test_tag_boost.py`
- Delete: `tests/unit/test_extraction_pre_filter.py`
- Modify: `tests/unit/test_relation_discovery.py`
- Modify: `tests/unit/test_resurrection.py`
- Modify: `tests/unit/test_query_expansion_settings.py`
- Modify: `tests/unit/test_settings_contract.py`

**Interfaces:**

- Delete `extract_pre_filter`, `tag_channel_enabled`, `tag_channel_weight`, `tag_candidate_limit`, and `relation_auto_apply_confidence` from `Settings` and config schema.
- Keep `tag_boost_enabled` and `tag_boost_weight`; tags remain a soft ranking signal over FTS/dense candidates.
- `RelationDiscoveryMode = Literal["off", "audit"]`.
- `discover_relations(..., mode, pool_limit, max_proposals) -> dict[str, int]` may insert validated proposals only; it never writes `memory_relations`, creates conflict cases, or changes Claim status.
- Default Query Expansion and Resurrection modes are `off`; explicit Query Expansion `auto` and explicit Resurrection `auto` remain supported.

- [ ] **Step 1: Add failing retirement tests**

Assert that v1 TOML containing each retired key fails as unknown and names `hl-mem config migrate`. Assert `Settings(relation_discovery_mode="auto").validate()` fails. Add a relation test where a `0.99` proposal in the only allowed `audit` mode leaves `memory_relations`, `conflict_cases`, and Claim statuses unchanged.

- [ ] **Step 2: Run focused tests and observe the retired behavior**

```powershell
uv run --frozen python -m pytest tests/unit/test_tag_boost.py tests/unit/test_extraction_pre_filter.py tests/unit/test_relation_discovery.py tests/unit/test_settings_contract.py -q --tb=short
```

Expected: the new retirement tests fail because the old settings and `auto` behavior still exist.

- [ ] **Step 3: Remove the independent Tag candidate channel**

Remove tag-channel fields from staged pipeline configs, candidate collection, RRF denominator, trace, and `RecallService` wiring. Retain query-tag extraction only when soft boost is enabled. Keep tests proving recognized tags can reorder existing candidates and cannot introduce a candidate absent from FTS/dense retrieval.

- [ ] **Step 4: Remove extraction pre-filter from Worker**

Delete the constructor injection, audit initialization, `_prepare_event()` skip branch, return payload, setting, implementation module, and dedicated tests. Event extraction continues through the existing admission and extractor validation paths; do not add a replacement filter.

- [ ] **Step 5: Make relation discovery audit-only**

Delete all branches that apply `AUTO_RELATIONS`, create contradiction conflict cases, or change endpoint status. Keep proposal validation, evidence checks, bounded neighbor selection, transactionality, idempotency, and proposal audit records. Remove no-longer-used thresholds from job handler wiring.

- [ ] **Step 6: Set safe automatic defaults and run the affected suites**

Set Query Expansion and Resurrection defaults to `off`. Update only tests that intentionally exercise those capabilities to opt in explicitly.

```powershell
uv run --frozen python -m pytest tests/unit/test_tag_boost.py tests/unit/test_relation_discovery.py tests/unit/test_query_expansion_settings.py tests/unit/test_resurrection.py tests/unit/test_worker.py tests/unit/test_settings_contract.py -q --tb=short
```

- [ ] **Step 7: Commit the product-surface retirement**

```powershell
git add -A src/hl_mem/config/models.py src/hl_mem/application/recall.py src/hl_mem/recall src/hl_mem/workers src/hl_mem/ingest/pre_filter.py tests/unit/test_tag_boost.py tests/unit/test_extraction_pre_filter.py tests/unit/test_relation_discovery.py tests/unit/test_query_expansion_settings.py tests/unit/test_resurrection.py tests/unit/test_worker.py tests/unit/test_settings_contract.py
git commit -m "refactor: retire low-value automatic config surfaces"
```

---

## Task 4: Build deterministic dry-run-first config migration

**Files:**

- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `src/hl_mem/config/migrate.py`
- Modify: `src/hl_mem/storage/backup.py`
- Modify: `src/hl_mem/cli.py`
- Create: `tests/fixtures/config/v0361-online.toml`
- Create: `tests/fixtures/config/v1-online.toml`
- Create: `tests/unit/test_config_migrate.py`
- Modify: `tests/unit/test_backup_cli.py`

**Interfaces:**

- Add runtime dependency `tomli-w==1.2.0`.
- `MigrationChange(path: str, before: object, after: object, reason: str)` and `MigrationPlan(source, target_version, document, changes, removed, blockers, database_path)` are frozen dataclasses.
- `plan_config_migration(config_path: Path, *, env_path: Path | None = None, environ: Mapping[str, str] | None = None) -> MigrationPlan` performs no write and validates the candidate against the caller's real secret sources.
- `apply_config_migration(plan, *, backup_path: Path | None, manifest_path: Path | None, env_path: Path | None = None, environ: Mapping[str, str] | None = None) -> Path` returns the config backup path and writes only when `plan.blockers` is empty and the candidate passes structural and runtime validation against those same secret sources.
- `validate_upgrade_recovery_set(database_path, backup_path, manifest_path) -> dict[str, object]` calls `validate_backup()` and verifies the live database's bound tombstone ledger identity matches the recovery snapshot.
- CLI syntax is `hl-mem config migrate --config PATH [--apply --backup PATH --manifest PATH]`; output is one deterministic JSON object for both dry-run and apply.

- [ ] **Step 1: Add failing pure migration tests**

Freeze a realistic v0.36.1 online fixture. Its expected v1 output must:

```toml
schema_version = 1

[recall]
query_expansion_mode = "off"
resurrection_mode = "off"

[relation]
discovery_mode = "audit"

[plugins]
enabled = []
```

The source fixture explicitly has Query Expansion/Resurrection/Relation `auto`, Tag channel keys, extraction pre-filter, and relation auto-apply confidence. Assert the plan reports every conversion/removal, returns byte-stable output on repeated runs, and does not alter the source.

Add blockers for explicit Fake extraction/embedding and unknown legacy keys. An already-v1 document produces a no-op plan and `--apply` refuses unnecessary rewriting.

- [ ] **Step 2: Run migration tests and observe missing implementation**

```powershell
uv run --frozen python -m pytest tests/unit/test_config_migrate.py -q --tb=short
```

Expected: collection fails because `hl_mem.config.migrate` does not exist.

- [ ] **Step 3: Implement pure transform and validated TOML emission**

Parse with `tomllib`; copy the mapping before transformation; never interpolate text. Emit with `tomli_w.dumps()`, parse the emitted bytes again with `tomllib.loads()`, then validate structure through the v1 loader. Runtime validation must use only the caller-provided process environment and `.env`; production migration code must never invent credentials. Preserve the exact original file as the rollback artifact because Tomli-W intentionally does not preserve comments.

Legacy transformations are fixed:

- absent root version becomes `1`;
- old Query Expansion and Resurrection effective `auto` become explicit `off`, including when omitted under old defaults;
- explicit relation discovery `auto` becomes `audit`; old `off` remains `off`;
- retired Tag channel, extraction pre-filter, and relation auto-apply keys are removed and reported;
- `[plugins].enabled = []` is added when absent;
- explicit Fake extraction/embedding blocks apply and names the exact replacement action.

- [ ] **Step 4: Add recovery-set proof before apply**

If the resolved configured database exists, require both backup and manifest. Validate checksum/integrity and compare `(ledger_id, schema_version)` from the backup report with the live database's `deletion_ledger_state`. If the database does not exist, report `recovery_required=false` and do not create it.

Back up config to `<name>.v0.bak`; refuse to overwrite an existing backup. Write the candidate to a same-directory temporary file, flush and `fsync`, reload it, then `os.replace()` the source. On any error, remove the temporary file and leave source/backup/database untouched.

- [ ] **Step 5: Wire the CLI and verify dry-run/apply semantics**

```powershell
uv run --frozen python -m pytest tests/unit/test_config_migrate.py tests/unit/test_backup_cli.py -q --tb=short
uv run --frozen hl-mem config migrate --config tests/fixtures/config/v0361-online.toml
```

Expected: tests pass; the manual command reports dry-run JSON and leaves the tracked fixture unchanged.

- [ ] **Step 6: Commit migration and dependency changes**

```powershell
git add pyproject.toml uv.lock src/hl_mem/config/migrate.py src/hl_mem/storage/backup.py src/hl_mem/cli.py tests/fixtures/config tests/unit/test_config_migrate.py tests/unit/test_backup_cli.py
git commit -m "feat: add verified v1 config migration"
```

---

## Task 5: Replace offline init with a service-neutral verified wizard

**Files:**

- Modify: `src/hl_mem/config/secrets.py`
- Modify: `src/hl_mem/daily_cli.py`
- Modify: `src/hl_mem/doctor.py`
- Modify: `tests/unit/test_daily_cli.py`
- Modify: `tests/unit/test_doctor.py`

**Interfaces:**

- Public syntax is `hl-mem init [--config PATH] [--env-file PATH] [--force]`; `--offline` is an argparse error.
- The wizard requires explicit LLM provider/base URL/model and Embedding base URL/model/dimension/API mode, then asks whether to enable the built-in reranker. No cloud vendor is silently selected.
- `merge_secret_file(path, updates, *, force=False) -> None` updates only supported key assignments, preserves unrelated lines, never prints values, writes atomically, and applies POSIX mode `0o600` where supported.
- `probe_model_components(settings) -> list[CheckResult]` checks enabled LLM, Embedding, and Reranker paths without writing application state.
- Config and secret files are committed only after structural/runtime validation and successful probes; an error leaves existing files unchanged.

- [ ] **Step 1: Replace the offline tests with failing wizard tests**

Inject prompt/getpass responses and a probe callback. Assert the generated TOML begins with `schema_version = 1`, contains no key value, sets Query Expansion/Resurrection/Relation to `off`, and loads through the production loader using the emitted `.env`.

Add tests for:

- `--offline` exits with code 2;
- an existing config requires `--force`;
- an existing `.env` preserves unrelated variables;
- prompt/probe failure changes neither file;
- secret values never appear in stdout, stderr, or exceptions.

- [ ] **Step 2: Run init tests and observe the obsolete offline behavior**

```powershell
uv run --frozen python -m pytest tests/unit/test_daily_cli.py -q --tb=short
```

Expected: the new tests fail because the existing command exposes `--offline` and writes static vendor-bound templates before validation.

- [ ] **Step 3: Implement explicit prompt collection and safe secret writes**

Keep interaction in `daily_cli.py`; keep validation, redaction, and file handling in `hl_mem.config`. Construct an in-memory `Settings`, validate it, probe required providers, render a minimal v1 TOML, validate the rendered bytes again, then write secrets and config atomically. If writing the second file fails, restore the first file from its same-operation backup.

- [ ] **Step 4: Verify successful and failed initialization paths**

```powershell
uv run --frozen python -m pytest tests/unit/test_daily_cli.py tests/unit/test_doctor.py tests/unit/test_config_loader.py -q --tb=short
```

- [ ] **Step 5: Commit the new first-run experience**

```powershell
git add src/hl_mem/config/secrets.py src/hl_mem/daily_cli.py src/hl_mem/doctor.py tests/unit/test_daily_cli.py tests/unit/test_doctor.py
git commit -m "feat: add service-neutral init wizard"
```

---

## Task 6: Make doctor a complete read-only v1 diagnostic

**Files:**

- Modify: `src/hl_mem/doctor.py`
- Modify: `src/hl_mem/cli.py`
- Modify: `tests/unit/test_doctor.py`
- Create: `tests/integration/test_doctor_readonly.py`

**Interfaces:**

- `CheckResult` adds a stable `code: str`; `to_dict()` returns `{"code", "status", "name", "detail"}`.
- `run_doctor(..., backup_path=None, manifest_path=None) -> list[CheckResult]` catches configuration errors and returns a `config` failure instead of aborting with a traceback.
- Doctor checks Python 3.12+, schema version, production readiness, provider connectivity/capability, database readability, migration count, FTS rebuild on a temporary copy, optional verified recovery set, daemon/Hermes contracts, and installed Hermes files.
- CLI supports `hl-mem doctor --json [--backup PATH --manifest PATH]`; text output remains human-readable.

- [ ] **Step 1: Add failing diagnostic-contract tests**

```python
def test_invalid_config_is_a_structured_failure(tmp_path: Path) -> None:
    path = tmp_path / "hl_mem.toml"
    path.write_text("schema_version = 2\n", encoding="utf-8")
    [result] = run_doctor(config_path=path, environ={})
    assert (result.code, result.status) == ("config", CheckStatus.FAIL)
```

Add tests that `--json` is valid JSON, Python 3.11 reports failure, missing recovery arguments produce one bounded warning, and supplied matching recovery evidence passes.

- [ ] **Step 2: Add an integration proof that doctor is locally read-only**

Create a valid v1 config/database/recovery set, record SHA-256 and directory entries for config, `.env`, database, WAL/SHM presence, tombstone ledger, backup, and manifest, run doctor with all network probes monkeypatched to deterministic read-only results, and assert the production paths and directory entries are unchanged. Ignore the system temporary directory because the FTS proof intentionally uses it.

- [ ] **Step 3: Run tests and observe current exception/mutation-contract gaps**

```powershell
uv run --frozen python -m pytest tests/unit/test_doctor.py tests/integration/test_doctor_readonly.py -q --tb=short
```

- [ ] **Step 4: Implement structured checks and JSON output**

Load with `validate_runtime=False`, report configuration structure and runtime readiness separately, reuse the same provider probe functions as init, and reuse `validate_upgrade_recovery_set()`. Do not auto-create a database when the configured path is missing.

- [ ] **Step 5: Run focused doctor and management CLI tests**

```powershell
uv run --frozen python -m pytest tests/unit/test_doctor.py tests/integration/test_doctor_readonly.py tests/unit/test_management_surfaces.py -q --tb=short
```

- [ ] **Step 6: Commit the diagnostic boundary**

```powershell
git add src/hl_mem/doctor.py src/hl_mem/cli.py tests/unit/test_doctor.py tests/integration/test_doctor_readonly.py
git commit -m "feat: make doctor a read-only v1 diagnostic"
```

---

## Task 7: Freeze the config contract and close Phase 2

**Files:**

- Create: `scripts/check_config_schema_snapshot.py`
- Create: `docs/config-schema.json`
- Modify: `scripts/generate_configuration_reference.py`
- Modify: `scripts/check_docs_consistency.py`
- Modify: `.github/workflows/test.yml`
- Modify: `docs/configuration.md`
- Modify: `docs/compatibility.md`
- Modify: `docs/CHANGELOG.md`
- Modify: `README.md`
- Modify: `README_EN.md`
- Modify: `docs/mcp.md`
- Modify: `config.example.toml`
- Modify: `tests/unit/test_startup_scripts.py`
- Modify: `tests/unit/test_config_loader.py`
- Modify: `tests/unit/test_daily_cli.py`

**Interfaces:**

- `build_config_schema() -> dict[str, object]` emits schema version, stable TOML paths, types, defaults, choices, required production fields, secret environment names, retired paths, and the open `plugins.<id>` namespace without secret values.
- `scripts/check_config_schema_snapshot.py` compares generated canonical JSON with `docs/config-schema.json`; `--write` performs the only snapshot update path.
- CI contract-snapshots runs config, OpenAPI, and MCP checks together.

- [ ] **Step 1: Add a failing snapshot check**

Implement the generator test first and run:

```powershell
uv run --frozen python scripts/check_config_schema_snapshot.py
```

Expected: failure because `docs/config-schema.json` does not exist.

- [ ] **Step 2: Generate and review the v1 config snapshot**

```powershell
uv run --frozen python scripts/check_config_schema_snapshot.py --write
uv run --frozen python scripts/check_config_schema_snapshot.py
```

Inspect the JSON and confirm retired keys, Fake runtime modes, and secret values are absent; `schema_version`, required production fields, and the plugin namespace are present.

- [ ] **Step 3: Regenerate current documentation and remove obsolete commands**

Update the configuration generator to consume `iter_config_fields()`. Regenerate `docs/configuration.md`; replace all `init --offline` examples with the verified wizard; document dry-run/apply/recovery-set migration and snapshot restore rollback. Record the breaking 0.x-to-1.x config behavior in the changelog and keep the existing 1.x compatibility policy unchanged.

- [ ] **Step 4: Add the config snapshot to CI and verify wheel CLI availability**

Extend `contract-snapshots` with the config check. In the existing clean-wheel build step, invoke:

```bash
.build-venv/bin/hl-mem --version
.build-venv/bin/hl-mem config migrate --help
.build-venv/bin/hl-mem doctor --help
```

This verifies empty installation and command packaging without making network calls or creating a database.

- [ ] **Step 5: Run the complete Phase 2 gate**

```powershell
uv run --frozen --extra sqlite-vec python -m pytest tests/unit/test_config_module_boundaries.py tests/unit/test_config_loader.py tests/unit/test_config_migrate.py tests/unit/test_daily_cli.py tests/unit/test_doctor.py tests/integration/test_doctor_readonly.py tests/unit/test_backup_cli.py -q --tb=short
uv run --frozen --extra sqlite-vec python -W error::ResourceWarning -m pytest tests/ -q --tb=short
uv run --frozen --extra sqlite-vec python -m ruff check src tests scripts
uv run --frozen --extra sqlite-vec python -m black --check .
uv run --frozen --extra sqlite-vec python -m isort --check-only .
uv run --frozen --extra sqlite-vec python -m mypy src/hl_mem --ignore-missing-imports
uv run --frozen --extra sqlite-vec python scripts/check_imports.py
uv run --frozen --extra sqlite-vec python scripts/check_docs_consistency.py
uv run --frozen --extra sqlite-vec python scripts/check_config_schema_snapshot.py
uv run --frozen --extra sqlite-vec python scripts/check_openapi_snapshot.py
uv run --frozen --extra sqlite-vec python scripts/check_mcp_snapshot.py
uv build
```

Expected: every command passes. Any intentional OpenAPI change caused by deleting a retired trace field is documented and its snapshot is updated in the same focused commit; otherwise OpenAPI and MCP snapshots remain byte-identical.

- [ ] **Step 6: Review scope and commit Phase 2 closeout**

```powershell
git diff --check
git status --short
git add .github/workflows/test.yml scripts/check_config_schema_snapshot.py scripts/generate_configuration_reference.py scripts/check_docs_consistency.py docs/config-schema.json docs/configuration.md docs/compatibility.md docs/CHANGELOG.md README.md README_EN.md docs/mcp.md config.example.toml tests/unit/test_startup_scripts.py tests/unit/test_config_loader.py tests/unit/test_daily_cli.py
git commit -m "docs: freeze the Core 1.0 config contract"
```

Confirm the branch contains no Provider Registry, usage ledger, automatic-job migration, new memory migration, Graph store, or unrelated directory reorganization.

---

## Phase 2 Completion Record

- [x] `hl_mem.settings` and `hl_mem.config_loader` are thin facades over eight typed config groups, one loader, one secret boundary, and one migrator.
- [x] Production TOML requires `schema_version = 1`, explicit model-backed extraction/embedding, and valid enabled secrets; Fake implementations remain test-only.
- [x] Tag soft boost remains; independent Tag candidates, extraction pre-filter, and relation auto-apply are absent from runtime and schema.
- [x] Query Expansion and Resurrection default off; migrated old effective `auto` becomes explicit `off`; explicit relation `auto` becomes audit-only.
- [x] Migration dry-run is deterministic and write-free; apply preserves the original config and requires matching verified recovery evidence for an existing database.
- [x] Init is service-neutral, validates/probes before commit, stores secrets only in `.env`, and exposes no offline/Fake option.
- [x] Doctor reports structured v1 readiness and is proven not to modify production configuration, secrets, database, ledger, backup, or plugin files.
- [x] Config schema snapshot, documentation, CI, clean-wheel CLI, backup/restore, focused tests, full suite, formatting, typing, import boundaries, OpenAPI, and MCP gates all pass.
- [ ] Only after this record is complete: author `2026-08-30-hl-mem-core-1-0-phase-3-provider-governance.md` against the merged Phase 2 code.
