# HL-Mem 1.1 Phase 3 External Provider and Recall Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the stable Provider Plugin API with an independently installable DashScope plugin and reduce Recall orchestration coupling without changing retrieval, ranking, delivery, or side-effect behavior.

**Architecture:** Build a separate distribution that implements only the public `hl_mem.plugins` contracts and leaves transport/governance to the host. In the core repository, add artifact-level conformance gates, then move the already-frozen query-planning state machine and side-effect coordination behind focused modules while keeping `RecallService` and documented monkeypatch surfaces intact.

**Tech Stack:** Python 3.12-3.14, hatchling, Entry Points, existing Provider contracts/registry/runtime, venv/pip artifact tests, pytest, SQLite, uv.

## Global Constraints

- Base is the merged Phase 2 commit on `develop/1.1`; record the SHA before work. The final `v1.0.0` line must be merged before the clean-install plugin gate that requires `hl-mem>=1.0,<2`.
- The external source repository is `D:\workspace\hl-mem-provider-dashscope`; distribution name is `hl-mem-provider-dashscope`; import package is `hl_mem_provider_dashscope`.
- Plugin ID is `hl-mem.provider.dashscope`; Entry Point group is `hl_mem.providers`; capability name is `dashscope_external` to avoid collision with the built-in `dashscope` key.
- Plugin version starts at `0.1.0`; manifest API version is the public `PROVIDER_API_VERSION`; `requires_hl_mem` is `>=1.0,<2`.
- The plugin implements stable LLM, Embedding, and Reranker only. It does not implement Image, routes, CLI, jobs, migrations, storage, retries, HTTP clients, budgets, audit, or metrics.
- Plugin production code imports only names re-exported by `hl_mem.plugins` plus Python standard-library modules. A static import gate enforces this.
- Built-in DashScope remains in core for 1.x compatibility. External failure cannot unregister or alter built-ins.
- Use Phase 1 live smoke and its remaining CNY 50 cycle budget. Do not duplicate paid fixtures or exceed the approved total.
- Recall refactoring starts only after Phase 2 entity behavior/evidence is committed. Characterization tests must pass unchanged before and after every move.
- Preserve `hl_mem.application.recall.RecallService`, `RecallRequest`, `_QueryExpansionSession`, `hybrid_claims`, repository symbols, time/sleep patch points, response ordering, Trace fields, Provider call counts, and transaction behavior.
- `hl_mem.recall` must not import application services. New query-planning code receives typed/scalar dependencies or structural protocols, not a DI container.
- Extend the existing `application/recall_side_effects.py`; do not create a second side-effect implementation.
- Lower complexity ceilings only to measured post-refactor values. Never increase another ceiling to make the phase pass.
- PyPI publication of either package and any GitHub repository/release creation require separate final user authorization.

---

## Task 1: Scaffold the Independent DashScope Provider Distribution

**Repository:** `D:\workspace\hl-mem-provider-dashscope`

**Files:**

- Create: `pyproject.toml`
- Create: `README.md`
- Create: `LICENSE`
- Create: `SECURITY.md`
- Create: `src/hl_mem_provider_dashscope/__init__.py`
- Create: `src/hl_mem_provider_dashscope/plugin.py`
- Create: `tests/test_manifest.py`
- Create: `tests/test_import_boundary.py`

**Interfaces:**

```toml
[project.entry-points."hl_mem.providers"]
"hl-mem.provider.dashscope" = "hl_mem_provider_dashscope:plugin"
```

```python
def plugin() -> ProviderPlugin:
    ...
```

- Manifest declares three `ProviderCapabilitySpec` entries named `dashscope_external` with stable LLM/Embedding/Reranker capabilities.
- `config_schema` is a local object schema with `additionalProperties=False`; it has no secret fields. Capability behavior comes from host `core_options`.
- The top-level package exports only `__version__` and `plugin`.
- README states trusted in-process execution, explicit allowlisting, host-owned credentials/transport/governance, compatibility range, and no sandbox claim.

- [ ] **Step 1: Create an isolated git repository and virtual environment**

Create the exact directory, initialize git with branch `main`, add a Python `.gitignore`, and create `.venv`. Do not nest it inside the core repository or copy the core `.env`.

- [ ] **Step 2: Write failing manifest and import-boundary tests**

```python
def test_plugin_declares_three_non_conflicting_stable_capabilities() -> None:
    manifest = plugin().manifest
    assert manifest.id == "hl-mem.provider.dashscope"
    assert {(item.capability.value, item.name) for item in manifest.capabilities} == {
        ("llm", "dashscope_external"),
        ("embedding", "dashscope_external"),
        ("reranker", "dashscope_external"),
    }
```

The AST import test rejects every `hl_mem.*` import except exact `hl_mem.plugins`.

- [ ] **Step 3: Run tests and observe the absent package/manifest**

```powershell
uv run --python 3.12 --with "hl-mem>=1.0,<2" --with pytest python -m pytest tests -q --tb=short
```

- [ ] **Step 4: Implement the minimal manifest and package metadata**

Factories may temporarily raise `NotImplementedError`; Task 2 replaces them. Manifest validation and Entry Point discovery must already succeed.

- [ ] **Step 5: Build, inspect metadata, and commit the scaffold**

```powershell
uv build
python -m zipfile -l dist/hl_mem_provider_dashscope-0.1.0-py3-none-any.whl
git add .
git commit -m "chore: scaffold the DashScope Provider plugin"
```

Expected: wheel contains only package metadata/source and no core source, secrets, databases, or benchmark output.

---

## Task 2: Implement the Three Neutral DashScope Adapters

**Repository:** `D:\workspace\hl-mem-provider-dashscope`

**Files:**

- Create: `src/hl_mem_provider_dashscope/llm.py`
- Create: `src/hl_mem_provider_dashscope/embedding.py`
- Create: `src/hl_mem_provider_dashscope/reranker.py`
- Modify: `src/hl_mem_provider_dashscope/plugin.py`
- Create: `tests/test_llm.py`
- Create: `tests/test_embedding.py`
- Create: `tests/test_reranker.py`
- Create: `tests/fixtures/*.json`

**Interfaces:**

- `DashScopeLLMAdapter` implements `LLMProviderAdapter`; supports JSON object but not strict JSON schema; builds OpenAI-compatible `/chat/completions`; forwards host `max_tokens` and `enable_thinking`; parses usage/request ID; recognizes only bounded 400/422 structured-mode errors.
- `DashScopeEmbeddingAdapter` implements compatible `/embeddings` and native `/api/v1/services/embeddings/text-embedding/text-embedding`; validates dimensions, response order, vector shape, and non-negative usage.
- `DashScopeRerankerAdapter` implements native `/api/v1/services/rerank/text-rerank/text-rerank`; validates index/finite score/token usage and never returns documents.
- Each adapter creates `ProviderRequest` and parses `ProviderResponse`; none imports/constructs httpx, reads environment variables, logs content, sleeps, retries, or mutates usage.

- [ ] **Step 1: Write golden request/response and malformed-response tests**

```python
def test_llm_request_is_transport_neutral() -> None:
    request = DashScopeLLMAdapter().build_request(ENDPOINT, INVOCATION)
    assert request.url.endswith("/chat/completions")
    assert request.json_body["enable_thinking"] is False
    assert "httpx" not in type(request).__module__
```

Cover both embedding modes, stable input order, rerank Top-N, missing/wrong envelopes, non-finite numbers, invalid indices, usage fields, secret-safe repr, and factory option handling.

- [ ] **Step 2: Run tests and observe adapter imports/factories failing**

```powershell
uv run --python 3.12 --with "hl-mem>=1.0,<2" --with pytest python -m pytest tests -q --tb=short
```

- [ ] **Step 3: Implement adapters only against `hl_mem.plugins`**

Copy no host proxy/transport code. Reimplement only the small vendor translation dictated by the public contract; use the core built-in behavior as parity evidence, not as an import dependency.

- [ ] **Step 4: Run tests, type/lint, build, and import scan**

```powershell
uv run --python 3.12 --with "hl-mem>=1.0,<2" --with pytest --with mypy --with ruff python -m pytest tests -q --tb=short
uv run --python 3.12 --with mypy python -m mypy src
uv run --python 3.12 --with ruff ruff check .
uv build
```

- [ ] **Step 5: Commit the adapters**

```powershell
git add src tests
git commit -m "feat: implement DashScope Provider adapters"
```

---

## Task 3: Add Core Artifact-Level Plugin Conformance Gates

**Repository:** core worktree

**Files:**

- Create: `scripts/check_external_provider_plugin.py`
- Create: `tests/integration/test_external_provider_plugin.py`
- Create: `tests/unit/test_external_provider_conformance.py`
- Modify: `docs/provider-plugins.md`
- Verify unchanged: `docs/provider-plugin-api.json`
- Modify: `.github/workflows/test.yml`

**Interfaces:**

```python
def inspect_plugin_wheel(core_wheel: Path, plugin_wheel: Path, *, python: Path) -> dict[str, object]: ...
```

- The checker creates a new temporary venv, installs the built core wheel and external plugin wheel, and runs a subprocess probe. It never imports the source checkout.
- Probe verifies disabled metadata filtering without `load()`, enabled discovery, manifest/API/core-version negotiation, Registry health, three governed proxies, and plugin-only failure isolation while built-ins remain registered.
- Conformance uses recording transport responses to compare the external adapter's request/parse semantics with the built-in DashScope provider for the approved fixture matrix; it does not require identical class names or internal implementation.
- A static wheel scan rejects imports outside `hl_mem.plugins`, bundled credentials, `.env`, databases, benchmark results, and core source.

- [ ] **Step 1: Write failing wheel and subprocess integration tests**

```python
def test_external_wheel_is_not_imported_until_allowlisted(built_wheels: WheelPair) -> None:
    result = run_clean_probe(built_wheels, enabled=())
    assert result["distribution_installed"] is True
    assert result["entry_point_loaded"] is False
```

Also test enabled health, capability lookup, collision fail-closed, incompatible core range, malformed config, transport failure settlement, zero dangling reservation, and built-in success after external failure.

- [ ] **Step 2: Build both wheels and observe missing checker/probe**

```powershell
uv run --frozen python -m build
Push-Location D:\workspace\hl-mem-provider-dashscope
uv build
Pop-Location
uv run --frozen python -m pytest tests/unit/test_external_provider_conformance.py tests/integration/test_external_provider_plugin.py -q --tb=short
```

- [ ] **Step 3: Implement clean-environment artifact checks**

Use `subprocess.run(..., check=False, capture_output=True, text=True)` with explicit argument arrays and a temporary environment that contains no production Provider keys. Bound probe output and delete the venv in `finally`.

- [ ] **Step 4: Run plugin and public-contract regressions**

```powershell
uv run --frozen python -m pytest tests/unit/test_external_provider_conformance.py tests/integration/test_external_provider_plugin.py tests/unit/test_provider_plugin_contracts.py tests/unit/test_provider_manifest.py tests/unit/test_provider_discovery.py tests/unit/test_provider_registry.py -q --tb=short
uv run --frozen python scripts/check_provider_plugin_api.py
```

The reference plugin must use the frozen contract unchanged. Any incompatible requirement is a failed Phase 3 gate and is reported as a separate API design defect; it is not silently folded into this implementation task.

- [ ] **Step 5: Commit core conformance gates**

```powershell
git add scripts/check_external_provider_plugin.py tests/integration/test_external_provider_plugin.py tests/unit/test_external_provider_conformance.py docs/provider-plugins.md .github/workflows/test.yml
git commit -m "test: verify an external Provider distribution"
```

---

## Task 4: Run the External Plugin Through the Real Provider Smoke

**Repositories:** core worktree and external plugin repository

**Files:**

- Create in core after success: `benchmarks/provider/results/1.1.0-dashscope-plugin-summary.json`
- Modify in core: `benchmarks/provider/README.md`
- Modify in plugin: `README.md`

**Interfaces:**

- Temporary config allowlists `hl-mem.provider.dashscope` and selects `dashscope_external` for LLM/Embedding/Reranker.
- TOML uses the quoted plugin namespace so dots stay inside the exact ID:

```toml
[plugins]
enabled = ["hl-mem.provider.dashscope"]

[plugins."hl-mem.provider.dashscope"]

[llm]
provider = "dashscope_external"

[embedding]
provider = "dashscope_external"

[reranker]
provider = "dashscope_external"
```

- The result uses Phase 1 schema with `provider_kind="external_plugin"`, plugin distribution/version/manifest ID, host/plugin wheel hashes, aggregate usage, checks, and zero active reservations.

- [ ] **Step 1: Prove remaining budget and build clean wheels**

Sum all prior 1.1 live evidence cost. Stop before network access if the next bounded run could exceed CNY 50. Build both wheels from clean trees and record their SHA-256 values.

- [ ] **Step 2: Install into a clean venv and run doctor/health without calls**

Verify disabled installation does not load plugin code, enabled config reports all three capabilities, and no usage ledger is created by doctor alone.

- [ ] **Step 3: Run the same live smoke through external capabilities**

```powershell
$smokeRoot = Join-Path ([IO.Path]::GetTempPath()) "hl-mem-1.1-plugin-smoke"
$smokeConfig = Join-Path $smokeRoot "hl_mem.toml"
$smokeEnv = "D:\workspace\hl_agent\hl_mem\var\provider-live.env"
$priceBook = Join-Path $smokeRoot "pricing.json"
uv run --frozen python benchmarks/provider/live_smoke.py --config $smokeConfig --env-file $smokeEnv --price-book $priceBook --output benchmarks/provider/results/1.1.0-dashscope-plugin-summary.json
```

On failure, count the attempt toward the budget; diagnose before any retry. Never fall back to the built-in Provider under the same result label.

- [ ] **Step 4: Compare governance and scan for leakage**

Assert all three capability calls have plugin ID `hl-mem.provider.dashscope`, usage matches actual attempts/items/documents, active reservations are zero, and built-in Registry health remains present. Run the repository secret/content scan over both git diffs and the artifact.

- [ ] **Step 5: Commit independent evidence in each repository**

Core:

```powershell
git add benchmarks/provider/results/1.1.0-dashscope-plugin-summary.json benchmarks/provider/README.md
git commit -m "docs: record external Provider evidence"
```

Plugin:

```powershell
git add README.md
git commit -m "docs: record HL-Mem integration evidence"
```

Do not publish either repository/package.

---

## Task 5: Extract Query Planning Behind the Recall Facade

**Repository:** core worktree

**Files:**

- Create: `src/hl_mem/recall/query_planning.py`
- Modify: `src/hl_mem/application/recall.py`
- Modify: `src/hl_mem/recall/__init__.py`
- Create: `tests/unit/test_recall_query_planning.py`
- Modify: `tests/unit/test_recall_characterization_v0293.py`
- Modify: `tests/unit/test_query_expansion.py`
- Modify: `tests/unit/test_entity_recall_integration.py`
- Modify: `scripts/complexity_budget.json`

**Interfaces:**

```python
@dataclass(frozen=True, slots=True)
class PreparedQueries:
    weighted_queries: tuple[WeightedQuery, ...]
    query_blobs: tuple[bytes, ...]
    entity_plan: QueryEntityPlan
    low_recall_expander: LowRecallExpander | None

class QueryPlanningSession:
    def __init__(self, service: RecallPlanningService, recall: RecallPlanningInput) -> None: ...
    def prepare(self) -> PreparedQueries: ...
```

- `RecallPlanningService`/`RecallPlanningInput` are structural protocols or frozen records containing only used members; `query_planning.py` does not import `hl_mem.application`.
- Move `_QueryExpansionSession`, expansion deadline/trigger handling, session-context lookup, embedding selection, and Phase 2 entity-plan composition without changing algorithms or calls.
- `application.recall._QueryExpansionSession` remains an alias/wrapper with the current `(service, recall)` constructor for patch compatibility.
- `RecallService._prepare_queries()` remains the application seam and returns the same observable data.

- [ ] **Step 1: Strengthen passing characterization tests before the move**

Assert exact `WeightedQuery` order/weights, embedding texts/counts, entity scope/fallback fields, expansion deadline outcomes, session-context Trace, low-recall callback behavior, and the `_QueryExpansionSession` import/constructor.

- [ ] **Step 2: Run characterization on the unmodified Phase 2 baseline**

```powershell
uv run --frozen python -m pytest tests/unit/test_recall_characterization_v0293.py tests/unit/test_query_expansion.py tests/unit/test_entity_recall_integration.py -q --tb=short
```

Expected: PASS. Correct fixtures before moving code if not.

- [ ] **Step 3: Add new-path tests and observe the missing module**

Test `QueryPlanningSession` directly with recording dependencies, then run `test_recall_query_planning.py`; expected collection failure before implementation.

- [ ] **Step 4: Move the state machine and leave compatibility wrappers**

Move code without editing prompt/query text, trigger rules, deadlines, exception classes, or Trace outcomes. Keep application-owned repository/service construction in `RecallService`.

- [ ] **Step 5: Run Recall/entity/query regressions and ratchet complexity**

```powershell
uv run --frozen python -m pytest tests/unit/test_recall_query_planning.py tests/unit/test_recall_characterization_v0293.py tests/unit/test_query_expansion.py tests/unit/test_entity_recall_integration.py tests/unit/test_search_trace.py -q --tb=short
uv run --frozen python scripts/check_complexity_budget.py --ratchet
```

Reduce the `application/recall.py` ceiling to the measured post-move size; add no allowance larger than measured formatted code.

- [ ] **Step 6: Commit the planning split**

```powershell
git add src/hl_mem/recall/query_planning.py src/hl_mem/recall/__init__.py src/hl_mem/application/recall.py tests/unit/test_recall_query_planning.py tests/unit/test_recall_characterization_v0293.py tests/unit/test_query_expansion.py tests/unit/test_entity_recall_integration.py scripts/complexity_budget.json
git commit -m "refactor: isolate recall query planning"
```

---

## Task 6: Move Recall Access and Exposure Coordination into the Existing Side-Effect Module

**Repository:** core worktree

**Files:**

- Modify: `src/hl_mem/application/recall_side_effects.py`
- Modify: `src/hl_mem/application/recall.py`
- Create: `tests/unit/test_recall_side_effect_coordinator.py`
- Modify: `tests/unit/test_p1_9_recall_side_effects.py`
- Modify: `tests/unit/test_recall_side_effects_deferred.py`
- Modify: `tests/unit/test_recall_characterization_v0293.py`
- Modify: `scripts/complexity_budget.json`

**Interfaces:**

```python
class RecallSideEffectCoordinator:
    def submit_exposures(self, query_id: str, exposures: list[tuple[Any, ...]]) -> int: ...
    def submit_access(self, query_id: str, claims: list[dict[str, Any]]) -> None: ...
    def record_access(self, claims: list[dict[str, Any]]) -> None: ...
    def run_with_retry(self, operation: Callable[[sqlite3.Connection], T]) -> T: ...
    def emit_failure(self, operation: str, outcome: str, error: Exception, claim_count: int) -> None: ...
```

- The coordinator receives connection/settings/sink/audit dependencies explicitly. It does not own recall selection, delivery, repositories outside side effects, or background thread lifecycle.
- `RecallService` retains thin `_submit_exposures`, `_submit_access`, `_record_access`, `_run_side_effect_with_retry`, and `_emit_failure` wrappers so existing patch points work.
- Preserve retry count/backoff, transaction begin/commit/rollback, deferred sink behavior, safe audit payload, error propagation/suppression, and access-count semantics exactly.

- [ ] **Step 1: Characterize wrappers, transactions, retries, and audits**

Add passing tests that monkeypatch each existing thin method and inspect commit/rollback, sleep sequence, error category, claim count, deferred/inline dispatch, and failure isolation.

- [ ] **Step 2: Add direct coordinator tests and observe the missing class**

```powershell
uv run --frozen python -m pytest tests/unit/test_recall_side_effect_coordinator.py -q --tb=short
```

- [ ] **Step 3: Move implementation and delegate from `RecallService`**

Do not move `recall_side_effect_health`, dispatcher thread ownership, packet feedback, or experience recall. Avoid a generic task runner; this coordinator is scoped to recall access/exposure semantics.

- [ ] **Step 4: Run side-effect and Recall regressions**

```powershell
uv run --frozen python -m pytest tests/unit/test_recall_side_effect_coordinator.py tests/unit/test_p1_9_recall_side_effects.py tests/unit/test_recall_side_effects_deferred.py tests/unit/test_recall_characterization_v0293.py tests/unit/test_context_packet.py tests/unit/test_context_packet_api.py -q --tb=short
uv run --frozen python scripts/check_complexity_budget.py --ratchet
```

Lower `application/recall.py` again if the measured ceiling decreases.

- [ ] **Step 5: Commit the side-effect split**

```powershell
git add src/hl_mem/application/recall_side_effects.py src/hl_mem/application/recall.py tests/unit/test_recall_side_effect_coordinator.py tests/unit/test_p1_9_recall_side_effects.py tests/unit/test_recall_side_effects_deferred.py tests/unit/test_recall_characterization_v0293.py scripts/complexity_budget.json
git commit -m "refactor: isolate recall side effects"
```

---

## Task 7: Close Phase 3 Across Both Repositories

**Files:**

- Modify in core: `docs/architecture.md`
- Modify in core: `docs/provider-plugins.md`
- Modify in core: `docs/CHANGELOG.md`
- Modify in plugin: `README.md`

- [ ] **Step 1: Run the external plugin repository gate**

```powershell
Push-Location D:\workspace\hl-mem-provider-dashscope
uv run --python 3.12 --with "hl-mem>=1.0,<2" --with pytest --with mypy --with ruff python -m pytest tests -q --tb=short
uv run --python 3.12 --with mypy python -m mypy src
uv run --python 3.12 --with ruff ruff check .
uv build
git status --short
Pop-Location
```

- [ ] **Step 2: Run the core Phase 3 gate**

```powershell
uv run --frozen python -m pytest tests/unit/ -q --tb=short
uv run --frozen python -m pytest tests/integration/test_external_provider_plugin.py -q --tb=short
uv run --frozen python -m ruff check .
uv run --frozen python -m black --check .
uv run --frozen python -m isort --check-only .
uv run --frozen python -m mypy src/hl_mem/ --ignore-missing-imports
uv run --frozen python scripts/check_imports.py
uv run --frozen python scripts/check_complexity_budget.py --ratchet
uv run --frozen python scripts/check_provider_plugin_api.py
uv run --frozen python scripts/check_openapi_snapshot.py
uv run --frozen python scripts/check_mcp_snapshot.py
uv run --frozen python -m build
```

- [ ] **Step 3: Review real-plugin lessons against the public API**

Record only proven lessons. If no stable API change was needed, say so. If an additive fix landed, link its contract test and compatibility note. Do not expand plugin scope to routes/jobs/storage.

- [ ] **Step 4: Commit Phase 3 documentation**

Core:

```powershell
git add docs/architecture.md docs/provider-plugins.md docs/CHANGELOG.md
git commit -m "docs: document proven Provider and Recall boundaries"
```

Plugin:

```powershell
git add README.md
git commit -m "docs: finalize plugin compatibility guidance"
```

Do not merge, push, or publish until the user chooses the integration path.
