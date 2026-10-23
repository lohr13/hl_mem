# HL-Mem 1.1 Phase 4 Extraction, Cleanup, and Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the approved responsibility split, remove misleading experimental residue, and produce an auditable 1.1 RC whose behavior, artifacts, live evidence, and 48-hour observation are release-ready.

**Architecture:** Keep `LLMExtractor` as the compatible facade and move only its extraction state machine and verification coordination into two focused internal modules using one shared run-state record. Remove the isolated PostgreSQL probe and stale current-capability claims without touching migration history. Extend the existing release gates with the 1.1 entity/ops/external-plugin evidence and a separate 48-hour RC observer.

**Tech Stack:** Python 3.12-3.14, existing LLM/Provider contracts, dataclasses, SQLite, pytest, uv/build, GitHub Actions, pip-audit, CycloneDX SBOM.

## Global Constraints

- Base is the merged Phase 3 commit on `develop/1.1`. Before RC preparation, `v1.0.0` must exist and its final `main` commit must be merged into `develop/1.1` with all conflicts resolved by preserving both 1.0 fixes and 1.1 features.
- Preserve exact prompt strings/hashes, response schemas, admission decisions, content/source boundaries, Provider request count/order, retry count, usage counters, exception classes, idempotency, transactions, and audit semantics.
- Preserve `LLMExtractor` constructor, `extract()`, public class attributes/properties, static helper patch points, and existing test-visible counters including `_schema_retry_count`.
- Reuse `prompts.py`, `schema.py`, `parsing.py`, `repair.py`, and `postprocessing.py` as single implementation sources. Do not copy their code into new modules.
- `orchestrator.py` owns chunking, auto split, soft split, delta repair, schema request retries, and merge flow. `verification.py` owns verifier scheduling, usage merge, and failure audit.
- A mutable `ExtractionRunState` is created once per `extract()` call. The facade exposes compatibility properties backed by that state; there is no second counter set.
- Do not change verification default/mode or make additional LLM calls. Refactoring must be behavior-neutral.
- Delete only the PostgreSQL connectivity probe and its exact missing-driver test. Keep the term PostgreSQL where it is remembered user content, a normalized product name, archived history, or a database-choice Claim value.
- Keep retired config-key recognition and migration fixtures for extraction pre-filter and independent Tag channel. Remove only current capability claims/imports/examples.
- No new production dependency, main-database migration, Provider capability, recall channel, or automatic task is allowed in this phase.
- RC version is `1.1.0rc1`. PyPI upload, GitHub tag/release, deployment, external plugin publication, and final `1.1.0` promotion each require explicit user authorization at the action boundary.
- RC observation is 48 elapsed hours on one immutable tag/commit. Any P0/P1, data-semantic, migration, stable-contract, or production-code fix creates `rc2` and restarts 48 hours; documentation-only corrections do not.
- Every task that changes tracked files ends in one reviewable commit. Verification and authorization-gated publication tasks create no extra commit after the RC candidate is frozen.

---

## Task 1: Freeze Extraction Orchestration and Verification Behavior

**Files:**

- Create: `tests/unit/test_extraction_orchestration_contract.py`
- Modify: `tests/unit/test_phase5_extraction_contract.py`
- Reuse: `tests/unit/test_extraction_chunking.py`
- Reuse: `tests/unit/test_entailment_verifier_unittest.py`
- Reuse: `tests/unit/test_llm_extractor.py`

**Interfaces:**

- Characterization records exact Provider request message hashes/schema hash, calls across success/schema retry/truncation split/claim overflow/soft split/delta repair/verifier, run counters after success and failure, audit action/outcome order, merged Claim order, and exception chains.
- It proves these current compatibility seams are active: `_extract_chunk_with_auto_split`, `_apply_delta_repair`, `_verify_extracted_claims`, `_extract_one_chunk`, `_request_delta_repair`, `_request_chunk`, `_parse_json`, `_claim`, and `_merge_chunk_claims`.
- It covers one Chinese and one English request and one multi-event source mapping case.

- [ ] **Step 1: Add characterization tests that pass before the move**

```python
def test_schema_retry_request_sequence_and_counters_are_frozen() -> None:
    extractor, client, audit = _retrying_extractor()
    claims = extractor.extract(SOURCE, CONTEXT)
    assert _request_hashes(client.requests) == EXPECTED_HASHES
    assert extractor._schema_retry_count == 1
    assert extractor.last_llm_call_count == 2
    assert _claim_projection(claims) == EXPECTED_CLAIMS
    assert _audit_projection(audit.events) == EXPECTED_AUDIT
```

Avoid snapshots containing raw source/model response. Use hashes and safe projections.

- [ ] **Step 2: Run the complete characterization cluster**

```powershell
uv run --frozen python -m pytest tests/unit/test_extraction_orchestration_contract.py tests/unit/test_phase5_extraction_contract.py tests/unit/test_extraction_chunking.py tests/unit/test_entailment_verifier_unittest.py tests/unit/test_llm_extractor.py -q --tb=short
```

Expected: PASS on the unmodified Phase 3 baseline. Correct the fixture, not production behavior, if an expectation is inaccurate.

- [ ] **Step 3: Commit the immutable safety net**

```powershell
git add tests/unit/test_extraction_orchestration_contract.py tests/unit/test_phase5_extraction_contract.py
git commit -m "test: freeze extraction orchestration behavior"
```

---

## Task 2: Extract Verification Coordination

**Files:**

- Create: `src/hl_mem/ingest/extraction/verification.py`
- Create: `src/hl_mem/ingest/extraction/run_state.py`
- Modify: `src/hl_mem/ingest/extraction/__init__.py`
- Modify: `src/hl_mem/ingest/llm_extractor.py`
- Create: `tests/unit/test_extraction_verification.py`
- Modify: `tests/unit/test_entailment_verifier_unittest.py`
- Modify: `tests/unit/test_extraction_orchestration_contract.py`
- Modify: `scripts/complexity_budget.json`

**Interfaces:**

```python
@dataclass(slots=True)
class ExtractionRunState:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    llm_call_count: int = 0
    schema_retry_count: int = 0
    repair_count: int = 0
    memorize_decisions: list[tuple[bool, str]] = field(default_factory=list)
    schema_errors: list[dict[str, Any]] = field(default_factory=list)
    secret_rejections: dict[str, int] = field(default_factory=dict)
    relation_metadata_counts: dict[str, int] = field(default_factory=dict)

class VerificationCoordinator:
    def verify(self, claims: list[ExtractedClaim], source_text: str, state: ExtractionRunState) -> list[ExtractedClaim]: ...
```

- Coordinator is configured with verifier, mode, thresholds, and audit getter. It returns Claims unchanged exactly as current audit/enforce behavior does.
- Empty long text, below-threshold claims, verifier exception, result-count mismatch, per-Claim audit, usage merge, and fail-open behavior remain identical.
- `LLMExtractor._verify_extracted_claims`, `_emit_verification_failure`, and `_record_verifier_usage` remain thin delegating wrappers with current signatures.
- The single `ExtractionRunState` initially backs verification counters; Task 3 moves the remaining counters onto it.

- [ ] **Step 1: Write failing direct coordinator and facade-delegation tests**

```python
def test_verifier_usage_merges_into_the_shared_run_state() -> None:
    state = ExtractionRunState()
    result = _coordinator(verifier=_verifier(tokens=(12, 5))).verify(CLAIMS, SOURCE, state)
    assert result == CLAIMS
    assert (state.input_tokens, state.output_tokens, state.total_tokens, state.llm_call_count) == (12, 5, 17, 1)
```

Also assert failure audit redaction/truncation, threshold decisions, empty-text audit, result mismatch, and wrappers delegate.

- [ ] **Step 2: Run tests and observe missing module/classes**

```powershell
uv run --frozen python -m pytest tests/unit/test_extraction_verification.py -q --tb=short
```

- [ ] **Step 3: Move verification logic without editing decisions or audit payloads**

The coordinator may use the existing `current_audit()` through an injected zero-argument getter for tests. Do not move the verifier model implementation in `ingest/verifier.py`.

- [ ] **Step 4: Run verifier and extraction characterization**

```powershell
uv run --frozen python -m pytest tests/unit/test_extraction_verification.py tests/unit/test_entailment_verifier_unittest.py tests/unit/test_extraction_orchestration_contract.py tests/unit/test_phase5_extraction_contract.py -q --tb=short
uv run --frozen python scripts/check_complexity_budget.py --ratchet
```

Lower `llm_extractor.py` to the measured size; do not preallocate room for Task 3.

- [ ] **Step 5: Commit the verification split**

```powershell
git add src/hl_mem/ingest/extraction/verification.py src/hl_mem/ingest/extraction/run_state.py src/hl_mem/ingest/extraction/__init__.py src/hl_mem/ingest/llm_extractor.py tests/unit/test_extraction_verification.py tests/unit/test_entailment_verifier_unittest.py tests/unit/test_extraction_orchestration_contract.py scripts/complexity_budget.json
git commit -m "refactor: isolate extraction verification"
```

---

## Task 3: Extract the Chunk/Retry/Repair State Machine

**Files:**

- Create: `src/hl_mem/ingest/extraction/orchestrator.py`
- Reuse: `src/hl_mem/ingest/extraction/run_state.py`
- Modify: `src/hl_mem/ingest/extraction/__init__.py`
- Modify: `src/hl_mem/ingest/llm_extractor.py`
- Create: `tests/unit/test_extraction_orchestrator.py`
- Modify: `tests/unit/test_extraction_orchestration_contract.py`
- Modify: `tests/unit/test_extraction_chunking.py`
- Modify: `tests/unit/test_extraction_repair.py`
- Modify: `tests/unit/test_softsplit_ab_equipment.py`
- Modify: `scripts/complexity_budget.json`

**Interfaces:**

```python
@dataclass(frozen=True, slots=True)
class ExtractionOrchestratorConfig:
    chunking_policy: ChunkingPolicy
    schema_retries: int
    structured_mode: StructuredOutputMode
    soft_split_enabled: bool
    delta_repair_enabled: bool

@dataclass(frozen=True, slots=True)
class ExtractionRunResult:
    claims: tuple[ExtractedClaim, ...]
    state: ExtractionRunState

class ExtractionOrchestrator:
    def extract(self, content: dict[str, Any] | str, context: dict[str, Any] | None = None) -> ExtractionRunResult: ...
```

- Dependencies are explicit, narrowly typed callbacks/adapters for LLM completion, Claim projection/admission, verification, schema/prompt helpers, and audit. Do not pass a service container or import application/storage modules.
- Move `extract` run initialization, `_extract_chunk_with_auto_split`, `_apply_delta_repair`, `_extract_one_chunk` orchestration, `_request_delta_repair`, `_request_chunk`, and run-summary emission.
- Keep compact Claim/domain projection helpers on `LLMExtractor` when they encode its product semantics; pass them as explicit callbacks rather than duplicating them.
- Facade `extract()` invokes the orchestrator and exposes the returned state through existing attributes/properties. Existing private orchestration methods remain thin wrappers where characterization proves a patch point.
- Exceptions preserve exact types/causes and state counters at the point of failure.

- [ ] **Step 1: Write failing direct state-machine tests**

```python
def test_truncation_split_preserves_call_order_and_merges_once() -> None:
    result = _orchestrator(_truncated_then_successful_client()).extract(SOURCE, CONTEXT)
    assert result.state.llm_call_count == 3
    assert _ids(result.claims) == EXPECTED_MERGED_IDS
    assert _audit_actions() == EXPECTED_SPLIT_ACTIONS
```

Cover schema retry, overflow split depth, soft split once, delta repair success/failure, no-memorize, multi-event indices, secrets/admission, error state, and provider request hashes.

- [ ] **Step 2: Run tests and observe the absent orchestrator**

```powershell
uv run --frozen python -m pytest tests/unit/test_extraction_orchestrator.py -q --tb=short
```

- [ ] **Step 3: Move one flow at a time behind unchanged facade wrappers**

Move and verify in this order: ordinary chunk request, schema retry, truncation/overflow split, soft split, delta repair, top-level run/logging. After each move, run the relevant single test before continuing.

- [ ] **Step 4: Run the broad extraction behavior cluster**

```powershell
uv run --frozen python -m pytest tests/unit -q -k "extract or chunk or schema or repair or entailment or admission or softsplit"
uv run --frozen python -m pytest tests/unit/test_extraction_orchestration_contract.py tests/unit/test_phase5_extraction_contract.py -q --tb=short
```

Expected: exact call/order/hash/counter/audit tests remain unchanged.

- [ ] **Step 5: Ratchet both module ceilings and import boundaries**

Measure formatted files. Reduce `llm_extractor.py` to its actual new size, add only a measured allowance for `orchestrator.py` if it exceeds the ordinary global limit, and run:

```powershell
uv run --frozen python scripts/check_complexity_budget.py --ratchet
uv run --frozen python scripts/check_imports.py
uv run --frozen python -m mypy src/hl_mem/ingest --ignore-missing-imports
```

- [ ] **Step 6: Commit the orchestration split**

```powershell
git add src/hl_mem/ingest/extraction/orchestrator.py src/hl_mem/ingest/extraction/__init__.py src/hl_mem/ingest/llm_extractor.py tests/unit/test_extraction_orchestrator.py tests/unit/test_extraction_orchestration_contract.py tests/unit/test_extraction_chunking.py tests/unit/test_extraction_repair.py tests/unit/test_softsplit_ab_equipment.py scripts/complexity_budget.json
git commit -m "refactor: isolate extraction orchestration"
```

---

## Task 4: Remove the PostgreSQL Probe and Correct Current Capability Documentation

**Files:**

- Delete: `src/hl_mem/storage/postgres.py`
- Modify: `tests/unit/test_production_boundaries.py`
- Modify: `docs/architecture.md`
- Modify: `docs/capability-matrix.md`
- Modify: `docs/CHANGELOG.md`
- Verify unchanged: `src/hl_mem/config/loader.py`
- Verify unchanged: `src/hl_mem/config/migrate.py`
- Verify unchanged: `docs/config-schema.json`
- Verify unchanged: `docs/archive/**`

**Interfaces:**

- No production import `hl_mem.storage.postgres` remains.
- Current architecture states SQLite authority and FTS + Dense + Tag soft boost accurately; it does not advertise an optional Tag channel.
- Current capability matrix removes PostgreSQL probe and extraction pre-filter rows, and identifies Tag only as a soft boost. Historical changelog/archive references stay intact.
- Retired paths `extraction.pre_filter`, `recall.tag_channel_enabled`, and `recall.tag_channel_weight` remain rejected with migration guidance.

- [ ] **Step 1: Prove the deletion scope with read-only searches**

Run targeted `rg` commands and classify every PostgreSQL/pre-filter/tag-channel hit as current capability, historical archive/changelog, retired-key recognition, or domain content. Record the list in the commit message body; do not delete classified history/domain content.

- [ ] **Step 2: Remove the probe and only its missing-driver test**

Delete `test_postgres_adapter_is_optional_and_reports_missing_driver()` and its import. Keep all other production-boundary tests.

- [ ] **Step 3: Correct current docs without rewriting history**

Edit the specific current architecture/capability lines. Changelog gets one new 1.1 entry saying the unused probe was removed; old release entries remain verbatim.

- [ ] **Step 4: Verify retired-key behavior and no live imports**

```powershell
uv run --frozen python -m pytest tests/unit/test_production_boundaries.py tests/unit/test_config_loader.py tests/unit/test_config_migrate.py tests/unit/test_tag_boost.py -q --tb=short
rg -n "hl_mem\.storage\.postgres|from .*postgres import|optional tag channel|deterministic pre-filter" src README.md docs/architecture.md docs/capability-matrix.md
```

Expected: pytest passes; `rg` returns no live import/current-capability claim. Archive/changelog/domain searches are intentionally not part of the zero-hit command.

- [ ] **Step 5: Commit the bounded cleanup**

```powershell
git add -u src/hl_mem/storage/postgres.py tests/unit/test_production_boundaries.py docs/architecture.md docs/capability-matrix.md docs/CHANGELOG.md
git commit -m "refactor: remove retired experimental residue"
```

---

## Task 5: Merge the Stable 1.0 Line and Prepare `1.1.0rc1`

**Files:**

- Modify: `pyproject.toml`
- Modify: `src/hl_mem/__init__.py`
- Modify: `uv.lock`
- Modify: `README.md`
- Modify: `docs/CHANGELOG.md`
- Create: `docs/release-checklist-1.1.md`
- Modify: `scripts/write_release_evidence.py`
- Create: `scripts/check_rc_observation_1_1.py`
- Modify: `.github/workflows/release-gates.yml`
- Create: `.github/workflows/rc-observation-1.1.yml`
- Modify: `tests/unit/test_write_release_evidence.py`
- Create: `tests/unit/test_rc_observation_1_1.py`

**Interfaces:**

- Version becomes exactly `1.1.0rc1` in project metadata and `hl_mem.__version__`; lock metadata, CLI, OpenAPI, MCP, health, and built wheel must agree.
- 1.1 release evidence adds required names `entity-recall`, `ops-report`, `provider-live-builtin`, and `provider-live-external` while retaining all 1.0 gates.
- `required_evidence_for(version: str) -> frozenset[str]` keeps the existing 1.0 set unchanged and adds the four 1.1 artifacts only for `1.1.0rcN`/`1.1.0`.
- `rc-observation-1.1.yml` resolves immutable tags matching `^v1\.1\.0rc[1-9][0-9]*$`, records commit/tag/run URL/UTC timestamp, runs quality/core/entity/ops/plugin/migration/security checks, and uploads a content-hashed JSON artifact.
- `scripts.check_rc_observation_1_1.evaluate(release, artifacts, issues, now) -> list[str]` enforces one tag/commit, passing gates, at least 48 elapsed hours, and no open P0/P1 created since publication.
- Promotion validator requires at least two passing observations whose timestamps span 48 hours on the same tag/commit and no unresolved P0/P1 recorded in the release checklist.
- Workflows prepare evidence only; they do not create tags, GitHub releases, PyPI uploads, or deploy services without separate authorization.

- [ ] **Step 1: Merge final `main` into `develop/1.1` after verifying `v1.0.0`**

Fetch tags read-only, prove `v1.0.0` resolves to the intended `main` commit, merge non-interactively, and run the Phase 3 targeted gate. Resolve conflicts by preserving 1.0 defect fixes and 1.1 contracts; never drop a side silently.

- [ ] **Step 2: Write failing version/evidence/observation tests**

```python
def test_release_evidence_requires_1_1_artifacts() -> None:
    assert "entity-recall" not in required_evidence_for("1.0.0")
    assert "entity-recall" in required_evidence_for("1.1.0rc1")


def test_promotion_requires_48_elapsed_hours_on_one_commit() -> None:
    assert validate_observations(_observations(hours_apart=47)) != []
    assert validate_observations(_observations(hours_apart=48)) == []
```

Also test tag regex, mixed commit rejection, failed gate, duplicate timestamp, version mismatch, and output hash.

- [ ] **Step 3: Run tests and observe missing 1.1 contracts**

```powershell
uv run --frozen python -m pytest tests/unit/test_write_release_evidence.py tests/unit/test_rc_observation_1_1.py tests/unit/test_check_rc_observation.py -q --tb=short
```

- [ ] **Step 4: Implement version/evidence/workflow changes and inspect diffs**

Update current release docs without rewriting 1.0 historical evidence. Pin every new Action to a full commit SHA. Keep existing 1.0 observation workflow intact.

- [ ] **Step 5: Verify metadata and workflows locally**

```powershell
uv lock
uv run --frozen python -m pytest tests/unit/test_write_release_evidence.py tests/unit/test_rc_observation_1_1.py tests/unit/test_check_rc_observation.py -q --tb=short
uv run --frozen python scripts/check_actions_pinned.py
uv run --frozen python -m build
$wheel = Get-ChildItem dist/*.whl | Sort-Object LastWriteTime -Descending | Select-Object -First 1
uv run --frozen python scripts/check_wheel_contents.py --reject-v030 $wheel.FullName
uv run --frozen python -c "import hl_mem, tomllib; assert hl_mem.__version__ == tomllib.load(open('pyproject.toml','rb'))['project']['version'] == '1.1.0rc1'"
```

- [ ] **Step 6: Commit RC metadata and gates**

```powershell
git add pyproject.toml src/hl_mem/__init__.py uv.lock README.md docs/CHANGELOG.md docs/release-checklist-1.1.md scripts/write_release_evidence.py scripts/check_rc_observation_1_1.py .github/workflows/release-gates.yml .github/workflows/rc-observation-1.1.yml tests/unit/test_write_release_evidence.py tests/unit/test_rc_observation_1_1.py
git commit -m "chore: prepare HL-Mem 1.1.0rc1"
```

---

## Task 6: Run the Full Local and Artifact Release Gates

**Artifacts outside the repository:**

- Create: `%TEMP%\hl-mem-1.1.0rc1-evidence\local-summary.json`
- Create: `%TEMP%\hl-mem-1.1.0rc1-evidence\entity-recall.json`
- Create: `%TEMP%\hl-mem-1.1.0rc1-evidence\public-recall.json`

This verification task intentionally creates no commit: the reviewed RC commit from Task 5 must remain immutable, and an evidence commit would make every embedded commit/hash stale.

- [ ] **Step 1: Run the authoritative full test and quality suite**

```powershell
uv run --frozen python -W error::ResourceWarning -m pytest tests/ -q --tb=short --cov=hl_mem --cov-report=term-missing --cov-fail-under=80
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
uv run --frozen python scripts/check_ops_report_schema.py
```

- [ ] **Step 2: Run migration, restore, default behavior, and benchmark gates**

```powershell
uv run --frozen python -m pytest tests/release/test_migration_release_gate.py tests/test_migration_upgrade.py tests/unit/test_backup_cli.py tests/release/test_default_zero_model_calls.py -q --tb=short
$releaseEvidenceRoot = Join-Path ([IO.Path]::GetTempPath()) "hl-mem-1.1.0rc1-evidence"
uv run --frozen python benchmarks/release/entity_v1.py --mode enforce --output (Join-Path $releaseEvidenceRoot "entity-recall.json")
$commit = git rev-parse HEAD
uv run --frozen python benchmarks/release/core_v1.py --label 1.1.0rc1 --commit $commit --output (Join-Path $releaseEvidenceRoot "public-recall.json")
uv run --frozen python benchmarks/release/compare_core_v1.py benchmarks/release/results/v1.0.0rc1.json (Join-Path $releaseEvidenceRoot "public-recall.json")
```

- [ ] **Step 3: Build and install both wheels in a clean environment**

Build core and external plugin from clean trees. Install artifacts—not source—into a new venv. Run version, `init --help`, `doctor`, `ops report` on disposable state, Provider discovery disabled/enabled, and the artifact conformance checker.

- [ ] **Step 4: Run supply-chain and leak checks**

Run pinned-action validation, `pip-audit`, CycloneDX SBOM generation, existing Gitleaks/history policy, wheel-content inspection, and focused scans over both live Provider summaries and release evidence. Any active secret or content leak blocks RC.

- [ ] **Step 5: Write and validate the local evidence summary**

Use real command outputs/artifact hashes. `%TEMP%\hl-mem-1.1.0rc1-evidence\local-summary.json` records the commit/version and each local gate status; it does not impersonate the remote GitHub Actions run required by `write_release_evidence.py`.

- [ ] **Step 6: Prove verification did not mutate the candidate**

```powershell
git status --short
git rev-parse HEAD
```

Expected: the worktree is unchanged from Task 5 and HEAD is the commit recorded in every local artifact. If any gate fails, do not write a passing summary and do not proceed to publication.

---

## Task 7: Authorization-Gated RC Publication and 48-Hour Observation

**External actions:** Git push/tag, GitHub prerelease, PyPI upload, deployment, scheduled observation.

- [ ] **Step 1: Stop and request explicit RC publication authorization**

Report the exact clean commit, version, local test counts, benchmark comparison, live-evidence cost, wheel hashes, and unresolved issues. Ask for one explicit authorization covering push, immutable `v1.1.0rc1` tag, GitHub prerelease, PyPI RC upload, and RC deployment. Do not infer it from design approval.

- [ ] **Step 2: Publish only the reviewed artifacts after approval**

Push `develop/1.1`, create an annotated immutable tag `v1.1.0rc1` at the reviewed commit, let the authorized publish workflow upload the exact built artifact, and verify PyPI/GitHub hashes and installed version.

- [ ] **Step 3: Deploy RC with backup and config/database checks**

Create/validate a backup and manifest, deploy the PyPI artifact to the intended test/production host using current schema-v1 config, run doctor, health, one synthetic write/recall, `ops report`, and verify no active reservations or errors. Do not overwrite `.env` or production keys.

- [ ] **Step 4: Collect two-plus observations spanning 48 elapsed hours**

Each observation must refer to the same tag/commit and run quality smoke, public/entity recall, ops schema/read-only behavior, external plugin conformance, migration, security, and installed version checks. Also inspect real service jobs/WAL/provider failures/cost and record only aggregate safe values.

- [ ] **Step 5: Apply the reset rule**

If a P0/P1 or qualifying code/contract/data fix is required, create `1.1.0rc2`, repeat Tasks 5-7, and restart the 48-hour clock. Documentation-only changes may retain the candidate when packaged behavior is unchanged.

- [ ] **Step 6: Stop and request separate stable-release authorization**

After 48 hours and zero unresolved P0/P1, report observation artifact links, exact RC commit/hash, PyPI install verification, and stable metadata diff. Ask separately before changing to `1.1.0`, tagging, publishing, releasing, or promoting deployment.

The phase is complete only after stable authorization is executed and post-release installation/health hashes are recorded, or when the user explicitly chooses to keep the validated RC without stable publication.
