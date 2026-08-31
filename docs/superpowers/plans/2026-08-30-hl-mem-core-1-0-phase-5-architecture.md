# HL-Mem Core 1.0 Phase 5 Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce the four proven maintenance hotspots without changing extraction, recall, HTTP, MCP, CLI, or stable evaluation behavior, and keep historical v0.30 research out of release artifacts.

**Architecture:** Keep the existing modular monolith and public import locations. `LLMExtractor`, `RecallService`, and `create_app()` remain compatibility facades while pure responsibilities move behind focused internal modules; route registration uses explicit factory dependencies rather than a container. Stable evaluation remains in `hl_mem.evaluation`, while version-specific research moves to a repository-only archive guarded by an artifact-content check.

**Tech Stack:** Python 3.12-3.14, Pydantic, FastAPI, SQLite, pytest, Ruff, Black, isort, mypy, `uv`, `build`, `zipfile`.

## Global Constraints

- Baseline is local `main` commit `df172ac`; do not push or publish from this phase.
- Preserve the untracked user file `docs/research/v028-plan-draft.md`; never stage, edit, move, or delete it.
- Preserve exact extraction outputs, provider request payloads, prompt hashes, recall ordering, trace content, HTTP schemas, OpenAPI, MCP, CLI, and stable `hl-mem eval` behavior.
- Preserve documented patch points in `docs/dev/patch-points-v0293.md`, including `api.server.RecallService`, `api.server.components`, `application.recall.hybrid_claims`, and the thin `RecallService` compatibility methods.
- Add no production dependency, DI framework, plugin framework, storage backend, recall channel, model call, or automatic background behavior.
- Archive only explicit v0.30 research. Stable evaluation code and state-lifecycle smoke gates remain production-supported.
- Use characterization-first TDD for moves: record behavior, make the move, and require the same test to pass unchanged.
- Lower complexity ceilings for every hotspot reduced in this phase; no ceiling may increase.
- Standard CI coverage floor remains at least 80%; no changed module may lose exercised behavior.

## File Structure

```text
src/hl_mem/
├── ingest/
│   ├── extraction/
│   │   ├── __init__.py          # internal extraction building blocks
│   │   ├── prompts.py           # prompt text, versions, hashes, language routing
│   │   ├── parsing.py           # response decoding, schema diagnostics, retry data
│   │   ├── postprocessing.py    # deterministic claim normalization and chunk merge
│   │   ├── repair.py            # deterministic JSON compatibility repair
│   │   └── schema.py            # extraction-only Pydantic wire schemas
│   └── llm_extractor.py         # public facade and orchestration only
├── application/
│   ├── recall.py                # RecallService orchestration and patch-point wrappers
│   ├── recall_enrichment.py     # batch evidence/relation/observation result enrichment
│   └── recall_delivery.py       # context candidates, bundles, freshness and packing
├── api/
│   ├── server.py                # app factory, lifecycle, middleware and shared factories
│   └── routes/
│       ├── __init__.py
│       ├── memory.py            # ingest, extraction and memory CRUD routes
│       ├── recall.py            # recall, bundle, packet and delivery-feedback routes
│       ├── experience.py        # episodes, feedback, traces and policies routes
│       └── maintenance.py       # consolidation, stats and job routes
└── evaluation/                  # stable, installed `hl-mem eval` implementation

benchmarks/archive/v030/         # repository-only historical experiment code and tests
scripts/check_wheel_contents.py  # built-artifact release boundary check
```

The old `hl_mem.ingest.schemas` and `hl_mem.ingest.repair` modules are retained as import-compatible re-export shims for 1.0 because they are already imported by tests and internal tooling. They contain no second implementation.

---

### Task 1: Freeze Phase 5 Behavior and Artifact Contracts

**Files:**
- Create: `tests/unit/test_phase5_extraction_contract.py`
- Create: `tests/unit/test_phase5_recall_contract.py`
- Create: `tests/unit/test_wheel_contents.py`
- Create: `scripts/check_wheel_contents.py`
- Modify: `.github/workflows/test.yml`
- Modify: `.github/workflows/publish.yml`

**Interfaces:**
- Consumes: existing `LLMExtractor`, `RecallService`, `create_app`, and wheel layout.
- Produces: characterization tests that remain unchanged during Tasks 2-6 and `check_wheel(path: Path, *, reject_v030: bool = False) -> list[str]`, returning human-readable violations. Task 6 changes the CLI and release workflows to use `reject_v030=True` after the archive move.

- [ ] **Step 1: Characterize extraction compatibility**

  Add tests that assert the current prompt constants and `PROMPT_HASH` are identical through `hl_mem.ingest.llm_extractor`, that `_parse_json`, `_schema_error_details`, `_claim`, and `_merge_chunk_claims` remain callable, and that a recording provider receives the exact current `complete()` keyword arguments for one Chinese and one English extraction.

- [ ] **Step 2: Characterize recall compatibility**

  Add tests that monkeypatch `application.recall.hybrid_claims` and the documented thin methods, then assert the same ordered `RecallResult`, retrieval bundle, materialized packet, access recording, and trace fields as before the split.

- [ ] **Step 3: Run the new characterization tests**

  Run:

  ```powershell
  uv run --frozen pytest tests/unit/test_phase5_extraction_contract.py tests/unit/test_phase5_recall_contract.py -q
  ```

  Expected: PASS on the unmodified baseline. These are characterization tests, so a failure means the fixture is inaccurate and must be corrected before refactoring.

- [ ] **Step 4: Write the failing wheel-boundary test**

  Test `check_wheel()` with a minimal zip fixture. Require `hl_mem/evaluation/runner.py` and reject members beginning with `hl_mem/evaluation/v030_` or `benchmarks/`. The test must fail because `scripts/check_wheel_contents.py` does not exist yet.

- [ ] **Step 5: Implement the artifact checker and CI invocation**

  Implement `check_wheel(path: Path, *, reject_v030: bool = False) -> list[str]` using `zipfile.ZipFile.namelist()`. The initial checker requires stable evaluation and rejects `benchmarks/`; Task 6 activates the v0.30 rejection after those modules move. The CLI accepts exactly one wheel path, prints every violation, and exits 1 on violations. After `python -m build`, the test workflow invokes it with the wheel produced under `dist/`; it must inspect the artifact rather than source strings.

- [ ] **Step 6: Run the checker tests and build smoke**

  Run:

  ```powershell
  uv run --frozen pytest tests/unit/test_wheel_contents.py -q
  uv run --frozen python -m build
  $wheel = Get-ChildItem dist/*.whl | Sort-Object LastWriteTime -Descending | Select-Object -First 1
  uv run --frozen python scripts/check_wheel_contents.py $wheel.FullName
  ```

  Expected: tests PASS, build succeeds, and the current wheel passes before research files move because the checker allows them only until Task 6 adds the explicit forbidden-member assertion against the real artifact.

- [ ] **Step 7: Commit the safety net**

  ```powershell
  git add tests/unit/test_phase5_extraction_contract.py tests/unit/test_phase5_recall_contract.py tests/unit/test_wheel_contents.py scripts/check_wheel_contents.py .github/workflows/test.yml
  git commit -m "test: freeze phase 5 behavior contracts"
  ```

### Task 2: Extract Prompt, Schema, and Repair Responsibilities

**Files:**
- Create: `src/hl_mem/ingest/extraction/__init__.py`
- Create: `src/hl_mem/ingest/extraction/prompts.py`
- Create: `src/hl_mem/ingest/extraction/schema.py`
- Create: `src/hl_mem/ingest/extraction/repair.py`
- Modify: `src/hl_mem/ingest/llm_extractor.py`
- Modify: `src/hl_mem/ingest/schemas.py`
- Modify: `src/hl_mem/ingest/repair.py`
- Modify: imports under `src/hl_mem/` and `tests/` that intentionally use the canonical internal paths

**Interfaces:**
- Consumes: existing prompt constants, prompt hash calculation, Pydantic extraction schemas, and JSON repair functions.
- Produces: canonical internal imports under `hl_mem.ingest.extraction`; the existing `llm_extractor`, `schemas`, and `repair` import surfaces re-export the same names.

- [ ] **Step 1: Add direct internal-module tests**

  Extend `test_phase5_extraction_contract.py` to import the prompt constants from both old and new paths and assert object equality, prompt-hash equality, schema JSON-schema equality, and repair output equality. Run it and confirm the new-path imports fail.

- [ ] **Step 2: Move prompts without editing their text**

  Move prompt strings, language routing constants, `compute_prompt_hash()`, `PROMPT_HASH`, and `LLM_EXTRACTOR_VERSION` into `extraction/prompts.py`. Re-export every previously imported prompt symbol from `llm_extractor.py`. Do not reformat prompt bodies because whitespace is part of the provider contract.

- [ ] **Step 3: Move extraction-only schemas and repair code**

  Move the implementation from `ingest/schemas.py` to `extraction/schema.py` and from `ingest/repair.py` to `extraction/repair.py`. Replace the old modules with explicit imports and `__all__`; do not copy implementations.

- [ ] **Step 4: Run prompt, schema, repair, and provider tests**

  Run:

  ```powershell
  uv run --frozen pytest tests/unit/test_phase5_extraction_contract.py tests/unit/test_llm_extractor.py tests/unit/test_extraction_language_episodic_time.py tests/unit/test_extraction_repair.py tests/unit/test_v021_repairs.py -q
  ```

  Expected: PASS with unchanged prompt hash, request payloads, schema diagnostics, and repaired payloads.

- [ ] **Step 5: Commit the structural move**

  ```powershell
  git add src/hl_mem/ingest/extraction src/hl_mem/ingest/llm_extractor.py src/hl_mem/ingest/schemas.py src/hl_mem/ingest/repair.py src/hl_mem tests
  git commit -m "refactor: isolate extraction contracts"
  ```

### Task 3: Extract Parsing and Post-processing Behind `LLMExtractor`

**Files:**
- Create: `src/hl_mem/ingest/extraction/parsing.py`
- Create: `src/hl_mem/ingest/extraction/postprocessing.py`
- Modify: `src/hl_mem/ingest/llm_extractor.py`
- Modify: `tests/unit/test_phase5_extraction_contract.py`
- Modify: `scripts/complexity_budget.json`

**Interfaces:**
- Consumes: `ExtractedClaim`, extraction schemas, deterministic repair, and prompt metadata from Task 2.
- Produces: pure functions `parse_json_response(raw: Any) -> dict[str, Any]`, `schema_error_details(error: Exception, payload: Any) -> list[dict[str, Any]]`, `claim_from_payload(item: dict[str, Any], *, preserve_subject: bool = False) -> ExtractedClaim`, and `merge_chunk_claims(chunks: list[list[ExtractedClaim]]) -> list[ExtractedClaim]`.

- [ ] **Step 1: Add delegation tests**

  Monkeypatch each new pure function and assert the matching `LLMExtractor` static method delegates with the same arguments and return value. Run the tests and confirm the imports fail before implementation.

- [ ] **Step 2: Move parsing and diagnostics**

  Move response decoding, deterministic JSON repair invocation, schema-error flattening, truncation diagnostics, and retry-data construction into `parsing.py`. Keep provider calls, retry decisions, logging, and orchestration in `LLMExtractor`.

- [ ] **Step 3: Move deterministic claim projection**

  Move item-to-claim projection, normalization, soft splitting, and chunk-claim merging into `postprocessing.py`. Keep `LLMExtractor._parse_json`, `_schema_error_details`, `_claim`, and `_merge_chunk_claims` as thin static wrappers with their current signatures.

- [ ] **Step 4: Run the extraction suite and verify no payload drift**

  Run:

  ```powershell
  uv run --frozen pytest tests/unit -q -k "extract or schema or repair or chunk or prompt"
  uv run --frozen pytest tests/unit/test_phase5_extraction_contract.py -q
  ```

  Expected: PASS; the recording-provider assertions are byte-for-byte unchanged.

- [ ] **Step 5: Lower the extractor complexity ceiling**

  Measure the new file and callable sizes with `scripts/check_complexity_budget.py`. Set the `llm_extractor.py` ceiling to the measured size rounded up only enough for deterministic formatting, and add focused entries only for new modules that exceed the normal global budget. Do not preserve the old 2,152-line allowance.

- [ ] **Step 6: Commit the extractor split**

  ```powershell
  git add src/hl_mem/ingest/extraction src/hl_mem/ingest/llm_extractor.py tests/unit/test_phase5_extraction_contract.py scripts/complexity_budget.json
  git commit -m "refactor: slim the LLM extractor facade"
  ```

### Task 4: Separate Recall Enrichment and Delivery

**Files:**
- Create: `src/hl_mem/application/recall_enrichment.py`
- Create: `src/hl_mem/application/recall_delivery.py`
- Modify: `src/hl_mem/application/recall.py`
- Modify: `tests/unit/test_phase5_recall_contract.py`
- Modify: `scripts/complexity_budget.json`

**Interfaces:**
- Consumes: repositories and domain models already used by `RecallService`.
- Produces: focused internal functions for batch evidence/replacement/relation/rival loading, observation/result assembly, context candidates, retrieval bundles, freshness packing, and packet materialization inputs. `RecallService` keeps the public orchestration method and documented patch-point wrappers.

- [ ] **Step 1: Strengthen compatibility tests before moving code**

  Assert that replacing `RecallService._assemble_results`, `_assemble_observations`, and `_materialize_context_packet` changes the service result, proving these wrappers remain active. Assert `application.recall.hybrid_claims`, `ClaimRepository`, `current_audit`, and `time.sleep` are still resolved from the compatibility module at runtime.

- [ ] **Step 2: Run the strengthened tests on the baseline**

  Run:

  ```powershell
  uv run --frozen pytest tests/unit/test_phase5_recall_contract.py tests/unit/test_recall_characterization_v0293.py -q
  ```

  Expected: PASS before the move.

- [ ] **Step 3: Extract enrichment functions**

  Move observation assembly and batch loading of evidence, replacements, relations, and rivals into `recall_enrichment.py`. Pass repositories or data explicitly; do not give the new module a connection factory or hidden service locator. Keep thin methods on `RecallService` for documented monkeypatch points.

- [ ] **Step 4: Extract delivery functions**

  Move context-candidate construction, bundle conversion, freshness calculations, and packed-context conversion into `recall_delivery.py`. Keep access recording, feedback attachment, resurrection, trace emission, and transaction-owning materialization in `RecallService`.

- [ ] **Step 5: Run the full recall behavior cluster**

  Run:

  ```powershell
  uv run --frozen pytest tests/unit/test_phase5_recall_contract.py tests/unit/test_recall_characterization_v0293.py tests/unit/test_relevance_gate.py tests/unit/test_recall_score_output.py tests/unit/test_session_context.py tests/unit/test_context_packet.py tests/unit/test_context_packet_api.py -q
  ```

  Expected: PASS with identical ordering, score output, packet contents, traces, and access effects.

- [ ] **Step 6: Lower the recall complexity ceiling and commit**

  Reduce the `application/recall.py` file ceiling to its new measured size and remove callable exceptions that no longer exist. Then run `uv run --frozen python scripts/check_complexity_budget.py --ratchet` and commit.

  ```powershell
  git add src/hl_mem/application/recall.py src/hl_mem/application/recall_enrichment.py src/hl_mem/application/recall_delivery.py tests/unit/test_phase5_recall_contract.py scripts/complexity_budget.json
  git commit -m "refactor: separate recall delivery internals"
  ```

### Task 5: Register HTTP Routes by Domain

**Files:**
- Create: `src/hl_mem/api/routes/__init__.py`
- Create: `src/hl_mem/api/routes/memory.py`
- Create: `src/hl_mem/api/routes/recall.py`
- Create: `src/hl_mem/api/routes/experience.py`
- Create: `src/hl_mem/api/routes/maintenance.py`
- Modify: `src/hl_mem/api/server.py`
- Modify: `tests/unit/test_phase5_api_contract.py`
- Modify: `scripts/complexity_budget.json`

**Interfaces:**
- Consumes: explicit callables supplied by `create_app()`: connection dependencies, `make_recall_service`, `execute_recall`, component factories, and settings.
- Produces: `add_memory_routes`, `add_recall_routes`, `add_experience_routes`, and `add_maintenance_routes`, each registering routes on the supplied `FastAPI` instance and returning `None`.

- [ ] **Step 1: Freeze application-factory patch points**

  Create `test_phase5_api_contract.py` with tests that monkeypatch `hl_mem.api.server.RecallService` before `create_app()` and assert the patched class serves recall, and monkeypatch `hl_mem.api.server.components.make_extractor` and assert dry-run extraction uses it. Assert every current operation ID and schema still matches the checked-in OpenAPI snapshot.

- [ ] **Step 2: Run the factory tests on the baseline**

  Run:

  ```powershell
  uv run --frozen pytest tests/unit/test_phase5_api_contract.py tests/unit/test_session_context.py tests/unit/test_dry_run_extract.py -q
  ```

  Expected: PASS before route extraction.

- [ ] **Step 3: Move memory routes**

  Move `/v1/events`, `/v1/events/batch`, `/v1/extract/dry-run`, and memory list/get/correct/save/forget registration into `routes/memory.py`. Pass `components.make_extractor` from the live `server` module during app creation so the documented monkeypatch remains effective.

- [ ] **Step 4: Move recall routes**

  Move public recall, internal retrieval-bundle, packet-materialization, and feedback-injected routes into `routes/recall.py`. Keep `make_recall_service` and `execute_recall` closures in `server.py`, where `RecallService` remains runtime patchable.

- [ ] **Step 5: Move experience and maintenance routes**

  Move episodes, feedback, traces, and policies into `routes/experience.py`; move consolidation, stats, and jobs into `routes/maintenance.py`. Keep health, lifespan, middleware, exception handlers, and conflict-route composition in `server.py`.

- [ ] **Step 6: Verify the HTTP contract**

  Run:

  ```powershell
  uv run --frozen pytest tests/unit/test_phase5_api_contract.py tests/unit/test_session_context.py tests/unit/test_dry_run_extract.py tests/unit/test_context_packet.py tests/unit/test_experience_api.py tests/unit/test_daily_memory_api.py tests/unit/test_forget_api.py -q
  uv run --frozen python scripts/check_openapi_snapshot.py
  uv run --frozen python scripts/check_mcp_contract_snapshot.py
  ```

  Expected: all PASS with no snapshot update.

- [ ] **Step 7: Lower the API complexity ceiling and commit**

  Reduce the `api/server.py` file ceiling and remove the obsolete `create_app` callable allowance. Run the complexity ratchet, then commit.

  ```powershell
  git add src/hl_mem/api/server.py src/hl_mem/api/routes tests/unit/test_phase5_api_contract.py scripts/complexity_budget.json
  git commit -m "refactor: organize HTTP routes by domain"
  ```

### Task 6: Archive Version-specific v0.30 Evaluation Research

**Files:**
- Create: `benchmarks/__init__.py`
- Create: `benchmarks/archive/__init__.py`
- Create: `benchmarks/archive/README.md`
- Create: `benchmarks/archive/v030/__init__.py`
- Move: `src/hl_mem/evaluation/v030_*.py` to `benchmarks/archive/v030/`
- Move: `scripts/run_v030_experiments.py` to `benchmarks/archive/v030/run_experiments.py`
- Move: `scripts/refreeze_v030_remote_evidence.py` to `benchmarks/archive/v030/refreeze_remote_evidence.py`
- Move: explicit `tests/unit/test_v030_*.py` and `tests/unit/test_v300_latest_wins_*.py` to `benchmarks/archive/v030/tests/`
- Move: `evaluation/tools/v0300_*.py` to `benchmarks/archive/v030/tools/`
- Modify: imports inside moved files
- Modify: `tests/unit/test_wheel_contents.py`
- Modify: `pyproject.toml`
- Modify: relevant evaluation documentation

**Interfaces:**
- Consumes: repository-only historical v0.30 experiment modules.
- Produces: an importable checkout archive under `benchmarks.archive.v030`, while installed `hl_mem.evaluation.runner.BenchmarkRunner` and `hl-mem eval` remain unchanged.

- [ ] **Step 1: Enumerate the move set and protect stable evaluation**

  Record the exact `v030_*` and `v0300_*` files in the archive README. Add tests asserting `hl_mem.evaluation.runner.BenchmarkRunner` imports and the CLI parser still exposes `eval`. Do not move `state_*`, `smoke_full_chain.py`, stable scorers, or tests such as `test_state_closeout_characterization.py` that protect production behavior.

- [ ] **Step 2: Move research code and repair only archive-local imports**

  Move the enumerated files with Git-aware moves. Update imports to `benchmarks.archive.v030...`. The archive README states that this code is historical, excluded from release artifacts and normal CI, and runnable only from a source checkout with its original optional dependencies.

- [ ] **Step 3: Exclude the archive from source distributions**

  Add `/benchmarks` to the source-distribution exclude list. Wheel packaging already includes only `src/hl_mem`; keep that simpler rule.

- [ ] **Step 4: Make the real-artifact assertion strict**

  Update `test_wheel_contents.py` and the checker CLI so release checking passes `reject_v030=True`: a built wheel must contain stable `hl_mem/evaluation/runner.py`, must not contain any `hl_mem/evaluation/v030_` member, and must not contain `benchmarks/`. Add the same strict checker invocation to both `.github/workflows/test.yml` and `.github/workflows/publish.yml`, so publishing cannot bypass the tested artifact boundary. Run the archive tests explicitly from the repository and the stable evaluation tests normally.

- [ ] **Step 5: Verify both repository and installed boundaries**

  Run:

  ```powershell
  uv run --frozen pytest tests/unit/test_benchmark.py tests/eval/test_state_lifecycle_scorer.py benchmarks/archive/v030/tests -q
  uv run --frozen python -m build
  $wheel = Get-ChildItem dist/*.whl | Sort-Object LastWriteTime -Descending | Select-Object -First 1
  uv run --frozen python scripts/check_wheel_contents.py $wheel.FullName
  ```

  Expected: historical tests pass from checkout, stable evaluation tests pass, and the real wheel contains stable evaluation but no v0.30 archive.

- [ ] **Step 6: Commit the evaluation boundary**

  ```powershell
  git add -A benchmarks src/hl_mem/evaluation scripts evaluation/tools tests/unit pyproject.toml docs .github/workflows/test.yml .github/workflows/publish.yml
  git reset docs/research/v028-plan-draft.md
  git commit -m "refactor: archive v0.30 evaluation research"
  ```

### Task 7: Close Phase 5 Quality Gates

**Files:**
- Modify: `scripts/complexity_budget.json`
- Modify: `docs/architecture.md`
- Modify: `docs/development.md`
- Modify: `docs/superpowers/plans/2026-08-30-hl-mem-core-1-0-roadmap.md`

**Interfaces:**
- Consumes: completed Tasks 1-6.
- Produces: accurate architecture documentation, reduced hotspot budgets, and a verified Phase 5 branch ready for fast-forward integration.

- [ ] **Step 1: Remove stale worker complexity debt**

  Because Phase 4 already moved deterministic maintenance to `workers/maintenance.py`, remove the stale `_run_maintenance` exception and reduce the `worker.py` ceiling to its measured current size. Do not reopen worker behavior in Phase 5.

- [ ] **Step 2: Document the resulting boundaries**

  Update architecture and development docs to state where extraction parsing/post-processing, recall enrichment/delivery, HTTP routes, stable evaluation, and archived research live. Update the roadmap Phase 5 status only after all gates pass.

- [ ] **Step 3: Run formatting, static, architecture, and contract gates**

  Run:

  ```powershell
  uv run --frozen ruff check .
  uv run --frozen black --check .
  uv run --frozen isort --check-only --gitignore .
  uv run --frozen mypy src
  uv run --frozen python scripts/check_complexity_budget.py --ratchet
  uv run --frozen python scripts/check_import_boundaries.py
  uv run --frozen python scripts/check_config_snapshot.py
  uv run --frozen python scripts/check_openapi_snapshot.py
  uv run --frozen python scripts/check_mcp_contract_snapshot.py
  uv run --frozen python scripts/check_docs.py
  ```

  Expected: every command exits 0. Complexity allowances for `llm_extractor.py`, `application/recall.py`, `api/server.py`, and `workers/worker.py` are strictly lower than at `df172ac`.

- [ ] **Step 4: Run the strict full suite**

  Run:

  ```powershell
  uv run --frozen pytest -q -W error::ResourceWarning
  ```

  Expected: zero failures, zero `ResourceWarning`, coverage at least 80%, and all contract subtests pass.

- [ ] **Step 5: Verify a fresh installed wheel**

  Build the wheel, inspect it with `check_wheel_contents.py`, install it into a newly created Python 3.13 virtual environment, and run:

  ```powershell
  hl-mem --help
  hl-mem eval --help
  python -c "from hl_mem.evaluation.runner import BenchmarkRunner; print(BenchmarkRunner.__name__)"
  ```

  Expected: all commands exit 0; the archive is not importable from the installed wheel.

- [ ] **Step 6: Inspect the exact diff and commit closure docs**

  Run `git diff df172ac --check`, `git diff --stat df172ac`, and `git status --short`. Confirm the user draft is the only untracked main-worktree file and is absent from every commit. Commit the documentation and final budget adjustments:

  ```powershell
  git add scripts/complexity_budget.json docs/architecture.md docs/development.md docs/superpowers/plans/2026-08-30-hl-mem-core-1-0-roadmap.md
  git commit -m "docs: close Core 1.0 phase 5"
  ```

- [ ] **Step 7: Integrate locally after final verification**

  Fast-forward local `main` to the Phase 5 branch only after the strict full suite and installed-wheel smoke pass on the final commit. Re-run the strict full suite on merged `main`, remove the isolated worktree and local Phase 5 branch, and do not push.

## Self-review Record

- **Spec coverage:** Extractor, Recall, API, Worker, stable evaluation, archived research, artifact composition, complexity ratchet, documentation, and merge verification each have an explicit task.
- **Scope control:** No provider/plugin work, Graph work, automatic behavior, database migration, public schema redesign, or unrelated directory move is included.
- **Compatibility:** All known v0.29.3 patch points and public 1.0 candidate surfaces remain at their current import locations.
- **Distribution:** The gate examines the built wheel and distinguishes stable evaluation from research; it is not a source-string assertion.
- **Type consistency:** Route registrars return `None`; wheel checking consistently uses `check_wheel(path: Path, *, reject_v030: bool = False) -> list[str]`; extractor helper signatures match the existing static methods.
- **Placeholder scan:** The plan contains no deferred decision or unspecified implementation branch.
