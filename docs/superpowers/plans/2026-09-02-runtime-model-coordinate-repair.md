# Runtime Model Coordinate Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair extraction-model write coordinates and publish the verified fix as `v1.1.0rc2`.

**Architecture:** A pure source-bounded helper repairs natural compact extraction without changing the LLM schema. A separate deterministic application projector records the effective extraction route at API startup and reuses existing typed entity, conflict, supersede, Evidence, and audit paths.

**Tech Stack:** Python 3.12–3.14, dataclasses, SQLite, FastAPI lifespan, pytest, uv, GitHub Actions, PyPI Trusted Publishing.

## Global Constraints

- Add zero LLM, Embedding, Reranker, or image calls.
- Add no database migration, table, worker, configuration key, or latest-wins slot.
- Never infer `task=extraction` without matching original Evidence.
- Never rewrite/delete historical Claims during installation or startup.
- Preserve all stable REST, MCP, CLI, Provider, configuration, and database contracts.

---

### Task 1: Source-bounded extraction-model coordinates

**Files:**
- Create: `src/hl_mem/ingest/extraction/model_coordinates.py`
- Modify: `src/hl_mem/ingest/llm_extractor.py`
- Test: `tests/unit/test_extraction_model_coordinates.py`

**Interfaces:**
- Produces: `project_extraction_model_coordinates(attribute, subject, value, evidence_quote) -> ModelCoordinateProjection`
- `ModelCoordinateProjection` contains `subject: str`, `task: str | None`, and `state_change: bool`.

- [ ] **Step 1: Write failing behavioral tests**

Cover these literal cases through the real compact postprocessor:

```python
def test_current_hl_mem_extraction_model_gets_stable_coordinate():
    claim = extract_one(
        subject="hl-mem 本地提取",
        value="hl-mem 本地提取当前实际使用 glm-5.3-flash",
        evidence="hl-mem 本地提取当前实际使用 glm-5.3-flash",
    )
    assert claim.subject == "hl_mem"
    assert claim.canonical_slot == "choice.model"
    assert claim.qualifiers == {"task": "extraction", "state_change": True}

def test_subject_task_without_evidence_does_not_create_slot(): ...
def test_non_hl_mem_extraction_subject_keeps_its_named_subject(): ...
def test_multiple_task_families_fail_closed(): ...
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `uv run --frozen pytest tests/unit/test_extraction_model_coordinates.py -q`

Expected: the positive case keeps the free-form subject and empty slot; negative tests describe existing conservative behavior.

- [ ] **Step 3: Implement the minimal pure projection**

Use a frozen alias table for the extraction family only. Match NFKC/casefolded aliases in Evidence and in subject/value. Normalize the subject only when it is exactly an HL-Mem alias plus a closed set of extraction/config decorators. Add `state_change` only when a currentness marker occurs in both value and Evidence.

- [ ] **Step 4: Verify GREEN and focused regressions**

Run:

```powershell
uv run --frozen pytest tests/unit/test_extraction_model_coordinates.py tests/unit/test_extraction_chunking.py tests/unit/test_extraction_batching.py tests/unit/test_active_claim_invariants.py -q
uv run --frozen ruff check src/hl_mem/ingest/extraction/model_coordinates.py src/hl_mem/ingest/llm_extractor.py tests/unit/test_extraction_model_coordinates.py
uv run --frozen mypy src/hl_mem/ingest/extraction/model_coordinates.py src/hl_mem/ingest/llm_extractor.py
```

- [ ] **Step 5: Commit**

```powershell
git add src/hl_mem/ingest/extraction/model_coordinates.py src/hl_mem/ingest/llm_extractor.py tests/unit/test_extraction_model_coordinates.py
git commit -m "fix: stabilize extraction model coordinates"
```

### Task 2: Deterministic extraction runtime projection

**Files:**
- Create: `src/hl_mem/application/runtime_config_report.py`
- Test: `tests/unit/test_runtime_config_report.py`

**Interfaces:**
- Produces: `report_extraction_runtime(db: sqlite3.Connection, settings: Settings, *, namespace: str = "default") -> RuntimeConfigReport`
- `RuntimeConfigReport` contains `claim_id`, `fingerprint`, `stored`, and `reason`; it contains no secret or source text.

- [ ] **Step 1: Write failing integration tests against a real temporary SQLite database**

Tests must prove:

```python
def test_runtime_report_supersedes_legacy_extraction_model_without_provider_calls(): ...
def test_runtime_report_is_idempotent_while_same_projection_is_active(): ...
def test_runtime_report_records_a_new_occurrence_after_model_change_and_rollback(): ...
def test_fake_extractor_profile_is_not_reported(): ...
```

Seed the old Claim as `hl_mem / choice.model / {"task":"extraction"}` with a v3 conflict key and `qwen3.7-plus`. Use literal assertions for the new active value `glm-5.3-flash`, the old `superseded` status, typed owner `project:hl_mem`, one bounded Event, and zero rows in Provider usage tables.

- [ ] **Step 2: Run the tests and verify RED**

Run: `uv run --frozen pytest tests/unit/test_runtime_config_report.py -q`

Expected: import failure because `runtime_config_report` does not exist.

- [ ] **Step 3: Implement the report through existing application services**

Build a SHA-256 fingerprint from non-secret effective route fields. Check only active/candidate/disputed runtime projections for idempotence. Insert a bounded Event, then call `IngestService.store_extracted` with:

```python
ExtractedClaim(
    predicate=SLOT_REGISTRY["choice.model"].predicate,
    value=settings.llm_model,
    subject="HL-Mem",
    qualifiers={
        "task": "extraction",
        "provider": settings.llm_provider,
        "state_change": True,
        "runtime_config": True,
        "config_fingerprint": fingerprint,
    },
    canonical_attribute="choice.model",
    canonical_slot="choice.model",
    scope="permanent",
    importance=0.9,
    assertion_kind="observation",
)
```

Use `FakeEmbedder(settings.embedding_dim)`. Never include API keys. Return `skipped/fake_profile` without opening a transaction for test profiles.

- [ ] **Step 4: Verify GREEN, storage invariants, and typing**

Run:

```powershell
uv run --frozen pytest tests/unit/test_runtime_config_report.py tests/unit/test_entity_resolution.py tests/unit/test_active_claim_invariants.py tests/unit/test_conflict_group_ingest.py -q
uv run --frozen ruff check src/hl_mem/application/runtime_config_report.py tests/unit/test_runtime_config_report.py
uv run --frozen mypy src/hl_mem/application/runtime_config_report.py
```

- [ ] **Step 5: Commit**

```powershell
git add src/hl_mem/application/runtime_config_report.py tests/unit/test_runtime_config_report.py
git commit -m "fix: project the active extraction route"
```

### Task 3: Startup wiring and production-shaped verification

**Files:**
- Modify: `src/hl_mem/api/server.py`
- Modify: `tests/unit/test_api_observability.py`
- Modify: `docs/CHANGELOG.md`
- Modify: `docs/architecture.md`
- Modify: `docs/HANDOFF.md`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: `report_extraction_runtime(...)` from Task 2.
- Produces: one idempotent startup projection before the API begins serving.

- [ ] **Step 1: Write a failing lifespan test**

Create a production-shaped Settings instance with a real extractor mode but no network-capable component invocation. Enter `TestClient(create_app(settings))`, then assert one typed active extraction-model Claim exists. Re-enter lifespan and assert Event/Claim counts do not grow.

- [ ] **Step 2: Run the lifespan test and verify RED**

Run: `uv run --frozen pytest tests/unit/test_api_observability.py -q`

Expected: no runtime configuration Claim exists after startup.

- [ ] **Step 3: Wire the projector into lifespan**

Call the projector immediately after `database.open_worker()` and before `yield`. Keep startup fail-loud: a failed authoritative database projection prevents the service from advertising readiness.

- [ ] **Step 4: Set RC2 identity and document the bounded behavior**

Set `pyproject.toml` to `1.1.0rc2`; update generated/static version references through the existing version sync command if present. Changelog must state that RC1 remains immutable and RC2 adds only this compatible repair.

- [ ] **Step 5: Verify production database on a disposable copy**

Copy `var/hl_mem.db` to a temporary path, point a temporary Settings snapshot at it, run one startup projection, and assert:

- the current typed extraction Claim is `glm-5.3-flash`;
- the compatible qwen extraction Claim is superseded;
- evaluation/reader/judge model Claims remain unchanged;
- the query “用什么 LLM 提取” returns `glm-5.3-flash` first;
- the source database checksum and modification time remain unchanged.

- [ ] **Step 6: Commit**

```powershell
git add src/hl_mem/api/server.py tests/unit/test_api_observability.py docs/CHANGELOG.md docs/architecture.md docs/HANDOFF.md pyproject.toml
git commit -m "release: prepare hl-mem 1.1.0rc2"
```

### Task 4: Full gates, integration, and immutable RC2 publication

**Files:**
- Verify only unless a gate exposes a defect.

- [ ] **Step 1: Run local release gates**

Run the repository’s full unit/release suite with strict `ResourceWarning`, coverage, Ruff, Black, isort, mypy, complexity, import boundaries, OpenAPI/MCP/config contracts, quality smoke, public recall comparator, migration/restore, build, SBOM/security, and clean wheel installs on available Python versions.

- [ ] **Step 2: Re-run the exact bug reproduction and recall gates fresh**

Expected:

- positive coordinate regression passes;
- 24-case entity fixture remains green;
- Core metrics remain Recall@1 `0.75`, Recall@5 `0.7917`, MRR `0.7639` within the frozen comparator;
- no-answer and forbidden-content regressions remain zero.

- [ ] **Step 3: Merge locally to `main` and verify the merged tree**

Fast-forward or merge the feature branch into local `main`, preserving the two pre-existing untracked files. Re-run the focused regression and build on the merged tree.

- [ ] **Step 4: Push the stable commit and wait for GitHub Tests/Security**

Push `main`. Do not tag until both remote workflows for that exact SHA succeed.

- [ ] **Step 5: Publish immutable `v1.1.0rc2`**

Create and push an annotated `v1.1.0rc2` tag at the verified SHA. Wait for Publish and release-gates to succeed; verify PyPI exposes both wheel and sdist for `1.1.0rc2`.

- [ ] **Step 6: Hand off deployment**

Report the commit/tag, local and remote evidence, PyPI status, and the expected first-start data transition. Hermes should deploy RC2 directly; RC1 need not be installed first.
