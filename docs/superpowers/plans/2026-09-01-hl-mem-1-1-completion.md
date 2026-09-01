# HL-Mem 1.1.0 Completion Implementation Plan

> **Execution requirement:** Implement sequentially with
> `superpowers:test-driven-development`; use
> `superpowers:verification-before-completion` before every completion claim.

**Goal:** Ship source trust and turn-taint propagation, session-kind admission,
Claim explanation, and the approved release hardening on top of the verified
1.1 candidate without adding model calls or breaking 1.x contracts.

**Architecture:** Add two Event provenance columns and one pure domain policy.
Hermes provides deterministic host metadata; the Worker applies the policy at
extraction and Claim-write boundaries; Evidence remains the single lineage
source. Explanation and Context rendering read provenance in bounded batch
queries. Release hardening remains isolated from memory semantics.

**Baseline:** `develop/1.1` at design commit `f512a94`.

## Global constraints

- Work only in the existing `develop/1.1` worktree and short child branches.
- Write a failing test before each production change.
- Preserve current Extraction prompt/schema and Provider call order/count.
- Add no LLM, Embedding, Reranker, network, Graph, or background-service call.
- Add only migration `060`, two Event columns, and `provenance.mode`.
- Never persist raw tool output in provenance metadata or diagnostics.
- Existing Event rows and old clients use `unknown` and exact 1.0 behaviour.
- Each task ends in one reviewable commit after its focused checks pass.
- Do not tag, publish, deploy to other machines, or upload PyPI/GitHub artifacts
  without explicit authorization at that action boundary.

---

## Task 1: Make disabled Query Expansion inert

**Files:**

- Modify: `tests/unit/test_query_expansion_settings.py`
- Modify: `tests/unit/test_config_loader.py`
- Modify: `src/hl_mem/config/models.py`
- Modify: `src/hl_mem/components.py`

- [ ] Add parameterized tests proving every incomplete dedicated Provider-line
  shape is accepted by `validate`, `validate_runtime`, and
  `make_query_expander` when mode is `off`.
- [ ] Add control tests proving `auto` and `always` still fail closed for the
  same incomplete values.
- [ ] Run the tests and confirm they fail on the current eager line resolution.
- [ ] Return early for disabled/max-zero construction before resolving line
  overrides; make runtime validation resolve them only for active modes.
- [ ] Preserve parked configuration and existing error messages for active
  modes.
- [ ] Verify:

```powershell
uv run --frozen python -m pytest tests/unit/test_query_expansion_settings.py tests/unit/test_config_loader.py -q --tb=short
uv run --frozen python -m mypy src/hl_mem/config/models.py src/hl_mem/components.py --ignore-missing-imports
```

- [ ] Commit: `fix: keep disabled query expansion inert`

---

## Task 2: Make Hermes environment ownership explicit

**Files:**

- Modify: `tests/unit/test_install_to_hermes.py`
- Modify: `src/hl_mem/adapters/hermes/deployment.py`
- Modify: `docs/hermes-integration.md` or the current Hermes setup document

- [ ] Add tests for install/upgrade/dry-run output with present and missing
  `<HERMES_HOME>/hl_mem.toml` and `<HERMES_HOME>/.env`.
- [ ] Assert output names the matching `doctor --config --env-file` invocation,
  states that repository `.env` is not used, and never prints/copies values.
- [ ] Implement path-only diagnostics in `DeploymentResult` rendering; do not
  inspect secret contents or require equality between environments.
- [ ] Verify installer idempotency and legacy-plugin notices remain unchanged.
- [ ] Run:

```powershell
uv run --frozen python -m pytest tests/unit/test_install_to_hermes.py -q --tb=short
uv run --frozen python scripts/check_docs_consistency.py
```

- [ ] Commit: `fix: clarify Hermes configuration ownership`

---

## Task 3: Detect stale or failed Hermes runtime registration

**Files:**

- Create: `src/hl_mem/adapters/hermes/runtime_status.py`
- Modify: `src/hl_mem/adapters/hermes/plugin/__init__.py`
- Modify: `src/hl_mem/adapters/hermes/deployment.py`
- Modify: `src/hl_mem/doctor.py`
- Create: `tests/unit/test_hermes_runtime_status.py`
- Modify: `tests/unit/test_hermes_discovery.py`
- Modify: `tests/unit/test_install_to_hermes.py`
- Modify: `tests/unit/test_doctor.py`

- [ ] Test identity capture for an installed wheel and editable Git checkout.
- [ ] Test atomic success/failure records, missing/malformed files, write
  failure, bounded failure count, and redaction of messages/URLs/configuration.
- [ ] Test plugin registration writes status without replacing the original
  registration exception.
- [ ] Test doctor outcomes: matching=OK, missing=WARN,
  mismatch/registration-failed/malformed=FAIL with restart guidance.
- [ ] Implement a versioned dataclass/typed mapping and atomic temporary-file
  replace under `<HERMES_HOME>/state/hl_mem-runtime.json`.
- [ ] Capture package version/path/Git SHA once at plugin import; store only PID,
  timestamps, status, count, and exception class in addition.
- [ ] Do not enumerate processes, claim the PID is alive, scrape logs, or add a
  heartbeat.
- [ ] Run:

```powershell
uv run --frozen python -m pytest tests/unit/test_hermes_runtime_status.py tests/unit/test_hermes_discovery.py tests/unit/test_install_to_hermes.py tests/unit/test_doctor.py -q --tb=short
uv run --frozen python -m mypy src/hl_mem/adapters/hermes src/hl_mem/doctor.py --ignore-missing-imports
```

- [ ] Commit: `feat: diagnose loaded Hermes runtime identity`

---

## Task 4: Add the Event provenance schema and pure domain contract

**Files:**

- Create: `src/hl_mem/storage/migrations/060_event_provenance.sql`
- Create: `src/hl_mem/domain/provenance.py`
- Modify: `src/hl_mem/api/schemas.py`
- Modify: `src/hl_mem/application/ingest.py`
- Modify: `src/hl_mem/cli.py`
- Modify: `src/hl_mem/config/models.py`
- Create: `tests/unit/test_migration_060_event_provenance.py`
- Create: `tests/unit/test_provenance_domain.py`
- Modify: `tests/unit/test_repository.py`
- Modify: `tests/unit/test_jsonl_import.py`
- Modify: `tests/unit/test_config_loader.py`
- Modify: `tests/test_migration_upgrade.py`
- Update intentionally: `docs/config-schema.json`
- Update intentionally: `docs/openapi.json`

- [ ] Add migration tests for an empty database and upgraded 1.0 database;
  assert exactly two non-null columns, closed values, `unknown` old rows, and no
  new index/table.
- [ ] Add pure-domain tests for enum validation, source aggregation, session
  policy, observe/enforce decisions, explicit-memory retention, and unknown
  legacy behaviour.
- [ ] Add Event API and idempotency tests proving provenance participates in the
  canonical payload and invalid values fail before storage.
- [ ] Add JSONL export/import tests so new archives preserve fields and legacy
  archives default both to `unknown`.
- [ ] Add `provenance.mode = enforce|observe`, default `enforce`, to the existing
  governance configuration composition and validation.
- [ ] Implement migration, domain types/decision record, Event input fields,
  canonical payload fields, and archive columns/defaults.
- [ ] Regenerate only the intentional config/OpenAPI snapshots:

```powershell
uv run --frozen python scripts/check_config_schema_snapshot.py --write
uv run --frozen python scripts/check_openapi_snapshot.py --update
```

- [ ] Verify:

```powershell
uv run --frozen python -m pytest tests/unit/test_migration_060_event_provenance.py tests/unit/test_provenance_domain.py tests/unit/test_repository.py tests/unit/test_jsonl_import.py tests/unit/test_config_loader.py tests/test_migration_upgrade.py -q --tb=short
uv run --frozen python scripts/check_config_schema_snapshot.py
uv run --frozen python scripts/check_openapi_snapshot.py
```

- [ ] Commit: `feat: add Event provenance contract`

---

## Task 5: Propagate deterministic Hermes turn provenance

**Files:**

- Create: `src/hl_mem/adapters/hermes/provenance.py`
- Modify: `src/hl_mem/adapters/hermes/provider.py`
- Create: `tests/unit/test_hermes_provenance.py`
- Modify: `tests/unit/test_hermes_discovery.py`

- [ ] Characterize the current two-Event batch and idempotency keys before
  adding fields.
- [ ] Add failing tests for interactive direct turns, cron turns, current-turn
  external tools, short external results, multiple tools, stale external tools
  before the latest user boundary, malformed messages, and unknown host data.
- [ ] Use only provider-initialization `platform`/context and message
  `_tool_output_risk`; permit the existing trusted wrapper as a compatibility
  fallback. Do not import Hermes private helpers or duplicate its tool-name
  allowlist.
- [ ] Scan backward to the latest user boundary, emit a bounded unique list of
  sanitised external tool names in Event metadata, and emit no raw result.
- [ ] Map known interactive user Event to `direct_user`, cron prompt Event to
  `system`, ordinary Assistant to `agent`, and an externally influenced final
  Assistant to `external_derived`; unsupported contexts use `unknown`.
- [ ] Preserve the two-Event batch size, idempotency keys, session cache
  invalidation, episode sync, and failure behaviour.
- [ ] Run:

```powershell
uv run --frozen python -m pytest tests/unit/test_hermes_provenance.py tests/unit/test_hermes_discovery.py -q --tb=short
uv run --frozen python -m mypy src/hl_mem/adapters/hermes --ignore-missing-imports
```

- [ ] Commit: `feat: preserve Hermes turn provenance`

---

## Task 6: Enforce source and session admission without extra model calls

**Files:**

- Modify: `src/hl_mem/domain/provenance.py`
- Modify: `src/hl_mem/workers/worker.py`
- Modify: `src/hl_mem/application/ingest.py`
- Create: `tests/unit/test_provenance_ingest.py`
- Modify: `tests/unit/test_extraction_batching.py`
- Modify: `tests/unit/test_worker.py`
- Modify: `tests/unit/test_ingest_transaction_characterization_v0293.py`

- [ ] Add failing Worker tests proving heartbeat/subagent Events stop before
  extraction in enforce mode, cron/external Events reach extraction, observe
  mode preserves current flow, and an old queued job is re-gated when handled.
- [ ] Add failing Claim-write tests for direct user, Agent, external,
  external-derived, system, cron, explicit memory, mixed evidence, and unknown.
- [ ] Assert external/system/cron results are low authority, inference remains
  inference, other assertions become observation, and retention becomes
  temporal unless existing explicit-memory protection applies.
- [ ] Assert Event/Evidence durability, transaction rollback, dedup/conflict
  behaviour, and `source_event_indices` mapping remain intact.
- [ ] Apply the pure decision once before paid extraction and again immediately
  before Claim construction, so pending work and direct callers cannot bypass
  policy.
- [ ] Record safe audit reason codes/counts only; do not record content.
- [ ] Compare Provider usage before/after and require zero new calls/tokens.
- [ ] Run:

```powershell
uv run --frozen python -m pytest tests/unit/test_provenance_ingest.py tests/unit/test_extraction_batching.py tests/unit/test_worker.py tests/unit/test_ingest_transaction_characterization_v0293.py -q --tb=short
uv run --frozen python scripts/check_complexity_budget.py --ratchet
```

- [ ] Commit: `feat: govern provenance-aware memory admission`

---

## Task 7: Add bounded Claim explanation

**Files:**

- Create: `src/hl_mem/application/claim_explanation.py`
- Modify: `src/hl_mem/cli.py`
- Create: `tests/unit/test_claim_explanation.py`
- Create: `tests/unit/test_explain_cli.py`

- [ ] Add query tests for active, superseded, expired, external, automated,
  unknown, mixed-evidence, missing, and dangling-evidence cases.
- [ ] Add human/JSON CLI tests with stable field order and exit behaviour.
- [ ] Add privacy tests for URL userinfo/query/fragment, secret-like metadata,
  raw Event content, malformed URI, and oversized metadata.
- [ ] Implement one read-only query/service that returns current persisted
  Claim state, direct Evidence, Event origin/session/time, sanitised source hint,
  and the current policy interpretation.
- [ ] State explicitly that output is a current explanation, not reconstructed
  historical admission after audit expiry.
- [ ] Add CLI parsers:

```text
hl-mem explain claim <claim-id>
hl-mem explain claim <claim-id> --json
```

- [ ] Ensure the command uses a read-only connection, never runs migration, and
  prints no content/tool body/configuration/secret.
- [ ] Run:

```powershell
uv run --frozen python -m pytest tests/unit/test_claim_explanation.py tests/unit/test_explain_cli.py -q --tb=short
uv run --frozen python -m mypy src/hl_mem/application/claim_explanation.py src/hl_mem/cli.py --ignore-missing-imports
```

- [ ] Commit: `feat: explain persisted Claim provenance`

---

## Task 8: Surface provenance safely in Context Packet and Hermes

**Files:**

- Modify: `src/hl_mem/storage/evidence.py`
- Modify: `src/hl_mem/application/recall_enrichment.py`
- Modify: `src/hl_mem/application/context_packet.py`
- Modify: `src/hl_mem/adapters/hermes/renderer.py`
- Modify: `tests/unit/test_context_packet.py`
- Modify: `tests/unit/test_hermes_renderer.py`
- Modify: `tests/integration/test_context_packet_feedback.py`

- [ ] Freeze byte-equivalent packet/renderer fixtures for direct-user and
  legacy-unknown Claims.
- [ ] Add failing tests that external/system/cron evidence receives bounded
  origin/session/time/source-hint metadata and one compact caution in rendered
  text.
- [ ] Add batch-query tests proving Top-K provenance enrichment executes one
  bounded query and introduces no per-item query loop.
- [ ] Add privacy and token-budget tests; raw URI, query, fragment, tool output,
  and metadata must not enter the model-visible string.
- [ ] Add a dedicated batch evidence-provenance repository method rather than
  changing generic evidence readers relied on by conflict/lifecycle code.
- [ ] Keep top-level Context Packet schema/version unchanged because
  `evidence[]` is already an open additive object; do not alter REST/MCP tool
  counts or response selection.
- [ ] Run:

```powershell
uv run --frozen python -m pytest tests/unit/test_context_packet.py tests/unit/test_hermes_renderer.py tests/integration/test_context_packet_feedback.py -q --tb=short
uv run --frozen python scripts/check_openapi_snapshot.py
uv run --frozen python scripts/check_mcp_snapshot.py
```

- [ ] Commit: `feat: render bounded memory provenance`

---

## Task 9: Close documentation and compatibility contracts

**Files:**

- Modify: `docs/architecture.md`
- Modify: `docs/configuration.md`
- Modify: `docs/compatibility.md`
- Modify: `docs/capability-matrix.md`
- Modify: `docs/CHANGELOG.md`
- Modify: `docs/hermes-integration.md` or current equivalent
- Modify: `tests/unit/test_compatibility_contracts.py`

- [ ] Document the two fields, `unknown` compatibility, observe/enforce modes,
  session matrix, no-fact-check boundary, explanation privacy, and forward-only
  migration/restore procedure.
- [ ] Mark provenance/session governance Beta for 1.1; keep the explanation
  command stable and read-only.
- [ ] Document that current Hermes produces interactive/cron automatically and
  never guesses heartbeat/subagent from prose.
- [ ] Record Query Expansion and Hermes runtime hardening in the changelog.
- [ ] Verify docs do not promise trust verification, sentence lineage,
  historical backfill, or automatic user confirmation.
- [ ] Run:

```powershell
uv run --frozen python scripts/check_docs_consistency.py
uv run --frozen python -m pytest tests/unit/test_compatibility_contracts.py -q --tb=short
```

- [ ] Commit: `docs: document HL-Mem 1.1 provenance governance`

---

## Task 10: Full regression and independent review

- [ ] Run focused feature clusters again.
- [ ] Run all local gates from a clean worktree:

```powershell
uv sync --frozen --extra sqlite-vec
uv run --frozen --extra sqlite-vec python -W error::ResourceWarning -m pytest tests/ -q --tb=short --cov=hl_mem --cov-report=term-missing --cov-fail-under=80
uv run --frozen ruff check .
uv run --frozen black --check .
uv run --frozen isort --check-only .
uv run --frozen python -m mypy src/hl_mem --ignore-missing-imports
uv run --frozen python scripts/check_imports.py
uv run --frozen python scripts/check_complexity_budget.py --ratchet
uv run --frozen python scripts/check_config_schema_snapshot.py
uv run --frozen python scripts/check_openapi_snapshot.py
uv run --frozen python scripts/check_mcp_snapshot.py
uv run --frozen python scripts/check_docs_consistency.py
uv build
```

- [ ] Run the frozen Core 1.0 and exact-entity release comparisons; require no
  forbidden/no-answer regression and no new Provider usage.
- [ ] Install the wheel into a fresh virtual environment and verify version,
  import, server startup, migration, CLI explanation, Provider plugin discovery,
  and Hermes thin-plugin import.
- [ ] Perform an independent code review of migration safety, authority/TTL
  semantics, queued-job gating, redaction, Context compatibility, and rollback.
- [ ] Fix every P0/P1 finding with a failing regression test and rerun affected
  plus full gates.
- [ ] Commit only if verification-generated tracked artifacts legitimately
  changed: `chore: finalize HL-Mem 1.1 completion gates`.

---

## Task 11: Local candidate observation and release

- [ ] Create and validate a pre-1.1 database backup, manifest, configuration
  backup, and restore command before upgrading the local service.
- [ ] Install the exact locally built candidate wheel and restart API, Worker,
  MCP, and Hermes gateway.
- [ ] During 24 hours, exercise and explain:
  - one direct interactive memory;
  - one external web/MCP-assisted turn;
  - one cron turn;
  - unknown/legacy Event compatibility.
- [ ] Inspect `hl-mem ops report`, failed/dead/running jobs, Worker liveness,
  database/WAL size, Provider usage, runtime identity, and redacted explanation
  output. Require no P0/P1 and zero provenance-induced model calls.
- [ ] Merge `develop/1.1` into `main`, push `main`, and wait for the existing
  GitHub Tests Linux/Python matrix to pass.
- [ ] Request explicit final release authorization.
- [ ] After authorization, tag the verified commit `v1.1.0`, push the tag, and
  verify PyPI/GitHub publication.
- [ ] In a clean environment install from PyPI and verify `--version`, health,
  migration, extraction, recall, `explain claim`, and Hermes registration.
- [ ] Record the published commit, workflow URLs, PyPI version, backup/rollback
  location, and final smoke evidence in the release report.

## Definition of done

- All three product features are observable and separately tested.
- Release hardening catches the three known upgrade/runtime traps.
- Existing exact-entity and Core 1.0 recall gates pass.
- Provenance introduces no model call and no raw external content leak.
- Database upgrade and documented backup restore are proven.
- The commit on PyPI is exactly the commit that passed GitHub Tests.
