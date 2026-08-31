# HL-Mem Core 1.0 Phase 3 Provider Governance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a governed, versioned Provider Plugin API whose LLM, Embedding, Reranker, and experimental Image calls all use host-owned transport and an atomic, persistent usage ledger.

**Architecture:** Keep `components.py` as the composition root, but resolve Provider adapters through one typed registry. External packages are discovered only through the `hl_mem.providers` Entry Point group and only when their exact plugin ID is present in `[plugins].enabled`; business code receives host proxies, never raw plugin adapters. A versioned SQLite sidecar at the existing `<database>.budget.db` path replaces the check-then-record token counter with atomic reservation and records every actual network attempt.

**Tech Stack:** Python 3.12-3.14, `importlib.metadata`, `packaging`, `jsonschema`, frozen dataclasses, runtime-checkable protocols, httpx, SQLite WAL, pytest, uv.

## Global Constraints

- Base commit is merged Phase 2 `a97e8a0`; implementation branch is `codex/core-1-0-phase-3` in `.worktrees/core-1-0-phase-3`.
- The approved design is `docs/superpowers/specs/2026-08-30-hl-mem-core-1-0-design.md`; this plan may refine signatures but cannot weaken its stable/experimental boundaries.
- Stable 1.x capabilities are exactly `llm`, `embedding`, and `reranker`. `image_describer` is experimental and must be labeled experimental in code, manifest validation, diagnostics, snapshots, and docs.
- Entry Point group is exactly `hl_mem.providers`; Provider Plugin API major is exactly `1`.
- External plugins are trusted in-process code, not a sandbox. Installation never activates a plugin; only `[plugins].enabled` does.
- Plugin ID and Provider names match `[a-z0-9][a-z0-9._-]{0,63}`. Duplicate entry points, IDs, capability keys, or built-in collisions fail before traffic is served.
- Plugin configuration is non-secret TOML under `[plugins.<id>]`. Capability credentials continue to come from the five Phase 2 secret variables; recursively secret-like plugin option keys are rejected.
- Plugins build neutral requests and parse neutral responses. They do not receive an httpx client and stable adapters may not perform hidden network retries.
- The host owns timeout, bounded retry, error normalization, safe response diagnostics, metrics, audit, usage reservation, and settlement.
- Budget accounting occurs per actual network attempt. Embedding is counted at `_request()` batch granularity, never again at `embed()` or `embed_batch()`.
- A reservation is released only when the host can prove no request was sent. Once `mark_attempt()` has committed, failure or crash recovery settles conservatively.
- `worker.daily_token_limit` remains the 0.x-to-1.0 token-limit key during this phase to avoid a second config migration. New request/cost/lease controls use the `[usage]` namespace.
- The usage sidecar reuses `<database>.budget.db`; it upgrades the old `token_budget` table once and does not add a main-memory SQLite migration.
- Built-in Providers and enabled external Providers use the same Registry, host transport, proxy, metrics, and ledger path. Fake test components never create a usage sidecar.
- Do not add pluggy, arbitrary hooks, plugin routes, CLI commands, jobs, migrations, storage backends, security-policy overrides, a DI framework, or a second plugin discovery mechanism.
- Do not change memory semantics, automatic worker scheduling, relation semantics, or the main database schema in this phase.
- Use TDD for every behavior change: observe the focused failure, implement the minimum behavior, rerun focused tests, run the relevant regression set, then commit.
- Preserve unrelated work and never stage `docs/research/v028-plan-draft.md`, `.coverage`, `Temp/`, `hl_mem.toml.bak_0820`, or `nul`.

---

## Task 1: Freeze the public Provider Plugin API types

**Files:**

- Create: `src/hl_mem/plugins/__init__.py`
- Create: `src/hl_mem/plugins/contracts.py`
- Create: `src/hl_mem/plugins/manifest.py`
- Reuse: `src/hl_mem/llm/types.py`
- Modify: `src/hl_mem/errors.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `tests/unit/test_provider_plugin_contracts.py`
- Create: `tests/unit/test_provider_manifest.py`

**Interfaces:**

- `PROVIDER_API_VERSION: Final[int] = 1` and `PROVIDER_ENTRY_POINT_GROUP: Final[str] = "hl_mem.providers"`.
- `ProviderCapability` has exact members `LLM="llm"`, `EMBEDDING="embedding"`, `RERANKER="reranker"`, and `IMAGE_DESCRIBER="image_describer"`; `ProviderStability` has `STABLE="stable"` and `EXPERIMENTAL="experimental"`.
- `ProviderEndpoint(base_url, api_key, model, timeout_seconds, max_attempts)` is frozen; `api_key` is `repr=False`.
- `ProviderRequest(method, url, headers, json_body, timeout_seconds)` is frozen; headers and body are `repr=False` and never enter errors or audit.
- `ProviderResponse(status_code, headers, json_body, attempts, request_id)` is frozen and transport-neutral.
- `ProviderCallError(category, message, attempts, sent, http_status=None, provider_code=None, request_id=None, response_body=None)` is the stable normalized failure and preserves the original transport error as `__cause__`.
- Capability invocations/results are frozen: `LLMInvocation`, `EmbeddingInvocation`, `EmbeddingResult`, `RerankInvocation`, `RerankResult`, `ValidatedImageInput`, and `ImageProviderResult`.
- Four runtime-checkable adapter protocols expose only `build_request(...) -> ProviderRequest`, `parse_response(ProviderResponse) -> capability result`, and capability-specific fallback inspection. No protocol exposes `httpx.Client`.
- `ProviderCapabilitySpec(name, capability, stability)` is frozen. LLM/Embedding/Reranker declarations must be stable; Image declarations must be experimental.
- `ProviderManifest(id, version, api_version, requires_hl_mem, capabilities, config_schema)` and `ProviderPlugin(manifest, factories)` are frozen public records.
- `validate_manifest(manifest, *, core_version) -> None` validates PEP 440 versions/specifiers, IDs, unique capability keys, stability, and a local object JSON Schema. Remote `$ref`, secret-like property names, and schemas without explicit `additionalProperties` are rejected.
- Add direct runtime dependencies `packaging>=24,<27` and `jsonschema>=4.23,<5`; remove `jsonschema` from the dev-only group to avoid duplicate ownership.

- [x] **Step 1: Write failing public-contract and manifest tests**

```python
def test_provider_request_repr_never_exposes_headers_or_body() -> None:
    request = ProviderRequest("POST", "https://example.test", {"Authorization": "Bearer secret"}, {"prompt": "private"}, 5.0)
    assert "secret" not in repr(request)
    assert "private" not in repr(request)


def test_image_capability_cannot_claim_stable_status() -> None:
    manifest = _manifest(capabilities=(ProviderCapabilitySpec("vision", ProviderCapability.IMAGE_DESCRIBER, ProviderStability.STABLE),))
    with pytest.raises(PluginManifestError, match="image_describer.*experimental"):
        validate_manifest(manifest, core_version="0.36.1")
```

Also cover invalid PEP 440, API major mismatch, unsatisfied `requires_hl_mem`, duplicate keys, remote `$ref`, secret-like config properties, and missing `additionalProperties`.

- [x] **Step 2: Run the tests and observe missing `hl_mem.plugins` modules**

Run:

```powershell
uv run --frozen python -m pytest tests/unit/test_provider_plugin_contracts.py tests/unit/test_provider_manifest.py -q --tb=short
```

Expected: collection fails because the public plugin package does not exist.

- [x] **Step 3: Implement immutable contracts and strict manifest validation**

Keep HTTP implementation details out of public types. Convert mappings to immutable copies at construction, bound diagnostic strings, and expose explicit `__all__` from `hl_mem.plugins`; do not re-export internal discovery or registry helpers.

- [x] **Step 4: Lock dependencies and run type/contract tests**

```powershell
uv lock
uv run --frozen python -m pytest tests/unit/test_provider_plugin_contracts.py tests/unit/test_provider_manifest.py tests/unit/test_llm_providers.py -q --tb=short
uv run --frozen python -m mypy src/hl_mem/plugins src/hl_mem/llm/types.py --ignore-missing-imports
```

- [x] **Step 5: Commit the public type surface**

```powershell
git add pyproject.toml uv.lock src/hl_mem/plugins src/hl_mem/llm/types.py src/hl_mem/errors.py tests/unit/test_provider_plugin_contracts.py tests/unit/test_provider_manifest.py
git commit -m "feat: define the Provider Plugin API"
```

---

## Task 2: Implement allowlisted discovery and a conflict-safe Registry

**Files:**

- Create: `src/hl_mem/plugins/discovery.py`
- Create: `src/hl_mem/plugins/registry.py`
- Create: `src/hl_mem/plugins/builtin.py`
- Modify: `src/hl_mem/config/models.py`
- Modify: `src/hl_mem/config/loader.py`
- Reuse: `src/hl_mem/settings.py`
- Modify: `src/hl_mem/components.py`
- Create: `tests/unit/test_provider_discovery.py`
- Create: `tests/unit/test_provider_registry.py`
- Modify: `tests/unit/test_config_loader.py`
- Modify: `tests/unit/test_config_module_boundaries.py`

**Interfaces:**

- `ProviderKey(capability, name)` is the only registry key; the same name may exist in two different capabilities.
- `discover_plugins(enabled, options, *, entry_points=None, core_version=__version__) -> tuple[ProviderPlugin, ...]` filters Entry Point metadata by exact name before `load()`. Disabled distributions are never imported.
- Each enabled ID must have exactly one Entry Point, and its loaded zero-argument factory must return a `ProviderPlugin` whose manifest ID equals the Entry Point name.
- `ProviderRegistry.register(plugin, *, builtin=False) -> None`, `freeze() -> None`, `keys() -> tuple[ProviderKey, ...]`, and package-internal typed adapter construction methods are deterministic and fail closed after freeze.
- `ProviderFactoryContext(key, core_options, plugin_options)` is frozen; the two option mappings are immutable, non-secret mappings. A factory has signature `Callable[[ProviderFactoryContext], ProviderAdapterProtocol]`.
- `build_provider_registry(settings, *, entry_points=None) -> ProviderRegistry` registers one built-in plugin first, then enabled external plugins, validates each plugin's options with its manifest JSON Schema, detects all conflicts, and freezes only after the complete set is valid.
- Built-in manifest ID is `hl-mem.builtin`. It registers current LLM names `dashscope`, `zhipu`, and `openai_compatible`, Embedding name `dashscope`, Reranker name `dashscope`, and experimental Image name `dashscope`.
- Provider selection fields become validated provider-name strings. Add `embedding.provider = "dashscope"`; keep it optional in `REQUIRED_RUNTIME_PATHS` so Phase 2 schema-v1 files remain loadable.
- Query Expansion uses the same LLM Registry and may select an external LLM name.
- `_split_plugin_namespace()` recursively rejects option keys containing `api_key`, `token`, `secret`, `password`, `authorization`, or `credential`, case-insensitively and across `-`/`_` variants.

- [x] **Step 1: Add failing discovery, conflict, and secret-option tests**

```python
def test_disabled_entry_point_is_not_loaded() -> None:
    disabled = FakeEntryPoint("unused.plugin", fail_on_load=True)
    assert discover_plugins((), {}, entry_points=(disabled,)) == ()
    assert disabled.load_calls == 0


def test_builtin_collision_fails_before_registry_freeze() -> None:
    external = _plugin("vendor.plugin", ProviderCapability.LLM, "dashscope")
    with pytest.raises(PluginConflictError, match=r"hl-mem\.builtin.*vendor\.plugin"):
        build_provider_registry(_settings_with(external), entry_points=(_entry_point(external),))
```

Also assert missing enabled IDs, duplicate distribution entry points, manifest/entry-point ID mismatch, unknown plugin config, invalid provider names, and nested `plugins.vendor.api_token` fail with paths and no values.

- [x] **Step 2: Run the focused tests and observe absent discovery/registry behavior**

```powershell
uv run --frozen python -m pytest tests/unit/test_provider_discovery.py tests/unit/test_provider_registry.py tests/unit/test_config_loader.py -q --tb=short
```

- [x] **Step 3: Implement metadata-first discovery and immutable Registry freeze**

Sort candidates by `(entry_point.name, distribution_name, entry_point.value)` only for deterministic diagnostics; never use order to resolve a collision. Validate all enabled IDs and all plugin options before returning a Registry.

- [x] **Step 4: Register built-ins through the same manifest and factory records**

Factories receive a frozen `ProviderFactoryContext` containing the capability-specific typed invocation settings and one plugin's validated non-secret options. They receive no transport, database connection, or application service.

- [x] **Step 5: Run config, registry, and existing factory regressions**

```powershell
uv run --frozen python -m pytest tests/unit/test_provider_discovery.py tests/unit/test_provider_registry.py tests/unit/test_config_loader.py tests/unit/test_config_module_boundaries.py tests/unit/test_llm_thinking_settings.py tests/unit/test_reranker_registry.py -q --tb=short
uv run --frozen python scripts/check_config_schema_snapshot.py --write
uv run --frozen python scripts/check_config_schema_snapshot.py
```

- [x] **Step 6: Commit discovery and registration**

```powershell
git add src/hl_mem/plugins src/hl_mem/config src/hl_mem/settings.py src/hl_mem/components.py tests/unit/test_provider_discovery.py tests/unit/test_provider_registry.py tests/unit/test_config_loader.py tests/unit/test_config_module_boundaries.py docs/config-schema.json
git commit -m "feat: add allowlisted Provider discovery"
```

---

## Task 3: Replace check-then-record budgeting with an atomic usage ledger

**Files:**

- Create: `src/hl_mem/observability/usage.py`
- Modify: `src/hl_mem/observability/__init__.py`
- Modify: `src/hl_mem/errors.py`
- Modify: `src/hl_mem/config/models.py`
- Modify: `src/hl_mem/ingest/budget.py`
- Reuse: `src/hl_mem/ingest/__init__.py`
- Create: `tests/unit/test_usage_governor.py`
- Modify: `tests/unit/test_budget.py`
- Modify: `tests/unit/test_settings_contract.py`
- Modify: `tests/unit/test_config_loader.py`
- Modify: `tests/unit/test_comprehensive_fixes.py`
- Modify: `docs/config-schema.json`

**Interfaces:**

- `UsageAmount(requests=0, input_tokens=0, output_tokens=0, embedding_items=0, rerank_documents=0, images=0, cost_microunits=None)` is frozen, non-negative, addable, and scalable. `total_tokens` is derived.
- `UsageLimits(daily_requests, daily_tokens, daily_cost_microunits)` treats values `<=0` as unlimited.
- `UsageIdentity(capability, operation, plugin_id, provider, model)` contains only low-cardinality non-secret labels.
- `UsageReservation(id, reserved, lease_expires_at)` is frozen.
- `default_usage_ledger_path(database_path) -> Path` returns the existing `<database>.budget.db` path.
- `UsageGovernor.reserve(identity, estimate) -> UsageReservation` uses `BEGIN IMMEDIATE` to include settled usage plus active reservations in the same budget decision.
- `mark_attempt(reservation_id) -> int` commits the next sent-attempt count before transport begins.
- `settle(reservation_id, actual, *, status, latency_ms, error_class=None)`, `release(reservation_id, *, reason)`, and `settle_unknown(reservation_id, *, status, latency_ms, error_class)` are idempotent but reject contradictory finalization.
- `recover_expired() -> dict[str, int]` releases only reservations with zero committed attempts; reservations with attempts become conservative `unknown` events using the reserved amount.
- `snapshot(day=None) -> dict[str, object]` reports settled, reserved, remaining, unknown-cost count, and counts by capability without exposing payloads.
- Sidecar schema version is independent of main migrations. Initialization migrates the old `token_budget` rows into one `legacy_worker_budget` event per date exactly once.
- New settings are `usage.daily_request_limit = 0`, `usage.daily_cost_limit_microunits = 0`, and `usage.reservation_lease_seconds = 300`; the existing `worker.daily_token_limit` supplies `UsageLimits.daily_tokens`.
- `TokenBudget` becomes a deprecated internal facade over `UsageGovernor` only until Task 5 removes its final callers; no production path may call `can_spend()` followed by `record_usage()` after Task 5.

- [x] **Step 1: Write failing atomicity, overrun, and recovery tests**

```python
def test_concurrent_reservations_cannot_both_spend_the_last_tokens(tmp_path: Path) -> None:
    governors = [UsageGovernor(tmp_path / "usage.db", UsageLimits(0, 10, 0)) for _ in range(2)]
    outcomes = _run_concurrently(lambda governor: governor.reserve(IDENTITY, UsageAmount(requests=1, input_tokens=7)), governors)
    assert sum(isinstance(item, UsageReservation) for item in outcomes) == 1
    assert sum(isinstance(item, UsageLimitExceededError) for item in outcomes) == 1


def test_expired_sent_reservation_is_settled_unknown_not_released(tmp_path: Path) -> None:
    governor = _governor(tmp_path, now=NOW)
    reservation = governor.reserve(IDENTITY, UsageAmount(requests=1, input_tokens=5))
    governor.mark_attempt(reservation.id)
    _advance_past_lease()
    assert governor.recover_expired() == {"released": 0, "settled_unknown": 1}
```

Also test actual usage above reservation, idempotent same finalization, contradictory finalization, natural-day reset, unlimited values, unknown cost, multi-process SQLite connections, and one-time legacy import.

- [x] **Step 2: Run tests and observe the check-then-record race**

```powershell
uv run --frozen python -m pytest tests/unit/test_usage_governor.py tests/unit/test_budget.py -q --tb=short
```

- [x] **Step 3: Implement the versioned sidecar and transaction protocol**

Use one fresh SQLite connection per public operation, `busy_timeout=5000`, WAL, `user_version`, exact integer counters, and UTC dates. Do not store prompt text, response text, URLs, headers, keys, plugin options, or image hashes.

- [x] **Step 4: Add settings, facade coverage, and schema snapshot**

```powershell
uv run --frozen python -m pytest tests/unit/test_usage_governor.py tests/unit/test_budget.py tests/unit/test_settings_contract.py tests/unit/test_config_loader.py -q --tb=short
uv run --frozen python scripts/check_config_schema_snapshot.py --write
uv run --frozen python scripts/check_config_schema_snapshot.py
```

- [x] **Step 5: Commit the atomic ledger**

```powershell
git add src/hl_mem/observability src/hl_mem/config/models.py src/hl_mem/ingest/budget.py src/hl_mem/ingest/__init__.py tests/unit/test_usage_governor.py tests/unit/test_budget.py tests/unit/test_settings_contract.py docs/config-schema.json
git commit -m "feat: add atomic Provider usage governance"
```

---

## Task 4: Build the host-owned transport and governed proxy primitive

**Files:**

- Create: `src/hl_mem/plugins/transport.py`
- Create: `src/hl_mem/plugins/proxies.py`
- Reuse: `src/hl_mem/http_utils.py`
- Modify: `src/hl_mem/monitoring/metrics.py`
- Reuse: `src/hl_mem/observability/audit.py`
- Modify: `src/hl_mem/observability/usage.py`
- Modify: `src/hl_mem/plugins/contracts.py`
- Create: `tests/unit/test_provider_transport.py`
- Create: `tests/unit/test_governed_provider_call.py`
- Create: `tests/unit/test_http_utils.py`
- Reuse: `tests/unit/test_llm_spans.py`
- Modify: `tests/unit/test_usage_governor.py`
- Modify: `tests/unit/test_provider_plugin_contracts.py`

**Interfaces:**

- `ProviderTransport(client=None).execute(request, *, max_attempts, on_attempt) -> ProviderResponse` is the only stable-capability network executor.
- `on_attempt(attempt_number)` runs and commits usage state immediately before each `httpx.Client.request()` call.
- Retry behavior remains bounded by `retry_http`: connection/timeouts plus 429/5xx retry; other 4xx do not retry; `Retry-After` is honored when valid.
- Transport raises normalized `ProviderCallError` with bounded redacted diagnostics and the original exception as cause. Request headers/body never appear in its message.
- `GovernedProviderCall(identity, governor, transport, metrics, audit).execute(request, estimate, parser) -> T` reserves for `estimate.scale(max_attempts)`, marks each attempt, executes, parses, and finalizes exactly once.
- On success, settlement is parser-reported actual usage plus the per-attempt estimate for prior failed attempts. On any failure after an attempt is marked, settlement is conservative. Adapter/request validation failures before the first marked attempt release the reservation.
- Provider metrics add `plugin_id`, `provider`, `model`, `attempts`, and usage counters while retaining existing health aggregation behavior.
- Audit contains only labels, counters, latency, safe error class/code, and reservation ID; it never stores prompt, documents, vectors, response body, credentials, or plugin options.

- [x] **Step 1: Write failing transport ownership and settlement tests**

```python
def test_retry_marks_and_accounts_for_each_actual_attempt() -> None:
    transport = _transport_that_times_out_once_then_succeeds()
    result = _governed(transport).execute(REQUEST, UsageAmount(requests=1, input_tokens=3), parse_success)
    assert result == "ok"
    assert _ledger_totals() == {"requests": 2, "input_tokens": 6}


def test_pre_send_adapter_failure_releases_reservation() -> None:
    with pytest.raises(ValueError, match="invalid request"):
        _governed().execute_factory(lambda: (_ for _ in ()).throw(ValueError("invalid request")), ESTIMATE, parse_success)
    assert _snapshot()["reserved"]["requests"] == 0
```

Cover 400 no retry, 429 retry, timeout exhausted, response JSON parse failure, actual-over-estimate, unknown outcome, secret redaction, and exactly one metric/audit final event per governed logical request.

- [x] **Step 2: Run focused tests and observe four paths bypass the absent host transport**

```powershell
uv run --frozen python -m pytest tests/unit/test_provider_transport.py tests/unit/test_governed_provider_call.py tests/unit/test_llm_spans.py -q --tb=short
```

- [x] **Step 3: Implement transport and the generic governed-call primitive**

Keep capability parsing outside transport. The common primitive accepts a parser callback that returns `(value, UsageAmount)` so LLM, Embedding, Reranker, and Image use one finalization algorithm without a generic hook system.

- [x] **Step 4: Run retry, diagnostics, metrics, and usage regressions**

```powershell
uv run --frozen python -m pytest tests/unit/test_provider_transport.py tests/unit/test_governed_provider_call.py tests/unit/test_http_utils.py tests/unit/test_monitoring.py tests/unit/test_audit.py tests/unit/test_usage_governor.py -q --tb=short
```

- [x] **Step 5: Commit the host transport**

```powershell
git add src/hl_mem/plugins/transport.py src/hl_mem/plugins/proxies.py src/hl_mem/monitoring/metrics.py src/hl_mem/observability/usage.py src/hl_mem/plugins/contracts.py tests/unit/test_provider_transport.py tests/unit/test_governed_provider_call.py tests/unit/test_http_utils.py tests/unit/test_usage_governor.py tests/unit/test_provider_plugin_contracts.py
git commit -m "feat: govern Provider HTTP transport"
```

---

## Task 5: Migrate LLM completion to Registry and UsageGovernor

**Files:**

- Modify: `src/hl_mem/llm/providers.py`
- Modify: `src/hl_mem/llm/client.py`
- Modify: `src/hl_mem/llm/types.py`
- Modify: `src/hl_mem/components.py`
- Modify: `src/hl_mem/workers/worker.py`
- Modify: `src/hl_mem/api/server.py`
- Modify: `src/hl_mem/mcp/server.py`
- Modify: `src/hl_mem/evaluation/extraction_ab.py`
- Delete: `src/hl_mem/ingest/budget.py`
- Modify: `src/hl_mem/ingest/__init__.py`
- Create: `tests/fixtures/providers/llm_openai_compatible.json`
- Create: `tests/unit/test_llm_provider_equivalence.py`
- Modify: `tests/unit/test_llm_client.py`
- Modify: `tests/unit/test_llm_providers.py`
- Modify: `tests/unit/test_worker.py`
- Modify: `tests/unit/test_extraction_batching.py`
- Modify: `tests/unit/test_comprehensive_fixes.py`
- Modify: `tests/test_e2e_real.py`
- Delete: `tests/unit/test_budget.py`

**Interfaces:**

- `ProviderRuntime(settings, registry, governor, transport)` is created once per API/MCP/Worker process and passed through component factories.
- `create_provider_runtime(settings, *, entry_points=None, client=None, create_usage=True) -> ProviderRuntime` validates plugin conflicts and recovers expired reservations before returning.
- `make_llm_client(..., runtime: ProviderRuntime | None = None) -> LLMClient` resolves the selected LLM adapter through Registry and always supplies governed transport for real clients.
- `LLMClient.complete()` keeps its current `LLMRequest -> LLMResponse` contract and structured JSON Schema-to-object fallback. Each actual primary/fallback HTTP sequence has a usage reservation and ledger event.
- LLM actual usage comes from Provider response fields. Missing usage settles the conservative estimate and marks usage status `estimated`.
- Existing `LLMSpanRecorder` remains a logical-call trace; the usage sidecar is the actual-attempt SSOT. They share reservation/trace identifiers but do not duplicate ledger ownership.
- Worker removes the outer `can_spend()/record_usage()` pair and its `TokenBudget` constructor dependency. Extractor result token fields remain for API responses and audit only.
- API, MCP, Worker, and evaluation build one runtime and reuse it. Fake `Settings.for_test()` components neither create the sidecar nor require a runtime.

- [ ] **Step 1: Freeze old LLM payload/response/error fixtures and add failing equivalence tests**

```python
@pytest.mark.parametrize("provider", ("dashscope", "zhipu", "openai_compatible"))
def test_registry_llm_matches_frozen_request_and_response(provider: str) -> None:
    fixture = _load_fixture(provider)
    client = _registry_client(provider, fixture["response"])
    assert client.complete(_request()) == fixture["normalized_response"]
    assert _captured_request() == fixture["request"]
```

Also assert structured fallback produces two governed actual calls, disabled/Fake paths produce zero usage records, and a worker extraction cannot overspend concurrently.

- [ ] **Step 2: Run LLM/Worker tests and observe direct provider construction and double budgeting**

```powershell
uv run --frozen python -m pytest tests/unit/test_llm_provider_equivalence.py tests/unit/test_llm_client.py tests/unit/test_worker.py tests/unit/test_extraction_batching.py -q --tb=short
```

- [ ] **Step 3: Convert built-in LLM adapters to neutral request/response methods**

Preserve provider-specific thinking controls, max tokens, response parsing, capabilities, request IDs, cached tokens, and structured fallback detection. Remove all direct `httpx.post` calls from `llm/client.py`.

- [ ] **Step 4: Introduce one ProviderRuntime per process boundary and remove TokenBudget callers**

Keep runtime injection optional only for tests and focused command helpers; production component construction must create or receive a persistent runtime. Do not use a process-global mutable Registry.

- [ ] **Step 5: Run LLM, Worker, API, MCP, and real-smoke regressions**

```powershell
uv run --frozen python -m pytest tests/unit/test_llm_provider_equivalence.py tests/unit/test_llm_client.py tests/unit/test_llm_providers.py tests/unit/test_worker.py tests/unit/test_extraction_batching.py tests/unit/test_comprehensive_fixes.py tests/unit/test_mcp_runtime.py tests/integration/test_extract_pipeline.py tests/test_e2e_real.py -q --tb=short
```

- [ ] **Step 6: Commit the LLM migration**

```powershell
git add -A src/hl_mem/llm src/hl_mem/components.py src/hl_mem/workers/worker.py src/hl_mem/api/server.py src/hl_mem/mcp/server.py src/hl_mem/evaluation/extraction_ab.py src/hl_mem/ingest/budget.py src/hl_mem/ingest/__init__.py tests/fixtures/providers tests/unit/test_llm_provider_equivalence.py tests/unit/test_llm_client.py tests/unit/test_llm_providers.py tests/unit/test_worker.py tests/unit/test_extraction_batching.py tests/unit/test_comprehensive_fixes.py tests/unit/test_budget.py tests/test_e2e_real.py
git commit -m "refactor: route LLM calls through Provider governance"
```

---

## Task 6: Migrate actual Embedding batches without double counting

**Files:**

- Modify: `src/hl_mem/ingest/embedder.py`
- Modify: `src/hl_mem/components.py`
- Modify: `src/hl_mem/protocols.py`
- Create: `tests/fixtures/providers/embedding_dashscope.json`
- Create: `tests/unit/test_embedding_provider_equivalence.py`
- Modify: `tests/unit/test_embeddings.py`
- Modify: `tests/unit/test_embedding_native_unittest.py`
- Modify: `tests/unit/test_reembed_all_claims_unittest.py`

**Interfaces:**

- `GovernedEmbedder` retains `EmbedderProtocol`, `embed_query`, and batching semantics.
- Registry resolves `embedding.provider`; the built-in DashScope adapter supports both existing `compatible` and `native` request envelopes.
- Each `_request(texts, text_type)` reserves and settles exactly once per actual batch. An 11-item `embed_batch()` records two requests and 11 embedding items, never an additional logical `embed_batch` event.
- Adapter output is validated for item count, stable ordering, numeric vectors, and configured dimension before packing BLOBs.
- Response token usage is recorded when present; otherwise input token amount and cost are explicitly unknown while request/item limits remain enforceable.

- [ ] **Step 1: Add failing compatible/native equivalence and batch-ledger tests**

```python
def test_eleven_embeddings_create_two_usage_events_not_three(runtime: ProviderRuntime) -> None:
    embedder = make_embedder(_settings(dim=2), runtime=runtime)
    assert len(embedder.embed_batch([str(index) for index in range(11)])) == 11
    assert runtime.governor.snapshot()["settled"] == {"requests": 2, "embedding_items": 11}
```

Also compare compatible/native URLs, bodies, ordering, dimension errors, retry behavior, query `text_type`, and normalized errors with frozen fixtures.

- [ ] **Step 2: Run tests and observe direct HTTP plus logical-only metrics**

```powershell
uv run --frozen python -m pytest tests/unit/test_embedding_provider_equivalence.py tests/unit/test_embeddings.py tests/unit/test_embedding_native_unittest.py -q --tb=short
```

- [ ] **Step 3: Implement the built-in adapter and governed Embedder facade**

Remove direct transport/retry/metrics code from `ingest/embedder.py`; retain deterministic `FakeEmbedder` unchanged and outside Registry.

- [ ] **Step 4: Run ingestion, recall, vector, and re-embedding regressions**

```powershell
uv run --frozen python -m pytest tests/unit/test_embedding_provider_equivalence.py tests/unit/test_embeddings.py tests/unit/test_embedding_native_unittest.py tests/unit/test_reembed_all_claims_unittest.py tests/unit/test_vector_backend_protocol.py tests/integration/test_e2e.py -q --tb=short
```

- [ ] **Step 5: Commit Embedding governance**

```powershell
git add src/hl_mem/ingest/embedder.py src/hl_mem/components.py src/hl_mem/protocols.py tests/fixtures/providers/embedding_dashscope.json tests/unit/test_embedding_provider_equivalence.py tests/unit/test_embeddings.py tests/unit/test_embedding_native_unittest.py tests/unit/test_reembed_all_claims_unittest.py
git commit -m "refactor: govern actual Embedding batches"
```

---

## Task 7: Migrate Reranker while preserving bounded recall fallback

**Files:**

- Modify: `src/hl_mem/recall/reranker.py`
- Modify: `src/hl_mem/components.py`
- Modify: `src/hl_mem/protocols.py`
- Create: `tests/fixtures/providers/reranker_dashscope.json`
- Create: `tests/unit/test_reranker_provider_equivalence.py`
- Modify: `tests/unit/test_reranker.py`
- Modify: `tests/unit/test_reranker_registry.py`
- Modify: `tests/unit/test_relevance_gate.py`

**Interfaces:**

- `GovernedReranker` retains `RerankerProtocol`, `last_outcome`, `last_error_class`, and `last_result` behavior.
- An empty document list returns immediately and records zero usage.
- One non-empty call records one request and `len(documents)` rerank documents, including retry attempt accounting.
- Invalid result indexes and malformed result envelopes remain contained as reranker errors; recall continues its existing RRF fallback without hiding the ledger/audit failure.
- `FakeReranker` remains test-only and outside Registry.

- [ ] **Step 1: Add failing request/result/fallback equivalence tests**

```python
def test_invalid_plugin_rerank_index_cannot_escape_host_validation(runtime: ProviderRuntime) -> None:
    reranker = _external_reranker(runtime, results=[(99, 1.0)])
    assert reranker.rerank("q", ["only"], 1) == []
    assert reranker.last_error_class == "InvalidResultIndex"
    assert runtime.governor.snapshot()["settled"]["rerank_documents"] == 1
```

Cover empty input, score ordering, 429/5xx retry, auth failure, malformed JSON, plugin collision, and recall fallback.

- [ ] **Step 2: Run tests and observe the current direct DashScope client**

```powershell
uv run --frozen python -m pytest tests/unit/test_reranker_provider_equivalence.py tests/unit/test_reranker.py tests/unit/test_reranker_registry.py tests/unit/test_relevance_gate.py -q --tb=short
```

- [ ] **Step 3: Implement Registry-backed Reranker and host validation**

Delete the local `RERANKER_PROVIDERS` mapping after the built-in adapter is registered in `plugins/builtin.py`; keep the public component factory as the only product assembly path.

- [ ] **Step 4: Run recall and health regressions**

```powershell
uv run --frozen python -m pytest tests/unit/test_reranker_provider_equivalence.py tests/unit/test_reranker.py tests/unit/test_reranker_registry.py tests/unit/test_relevance_gate.py tests/unit/test_p1_1_component_degradation.py tests/integration/test_e2e.py -q --tb=short
```

- [ ] **Step 5: Commit Reranker governance**

```powershell
git add src/hl_mem/recall/reranker.py src/hl_mem/components.py src/hl_mem/protocols.py src/hl_mem/plugins/builtin.py tests/fixtures/providers/reranker_dashscope.json tests/unit/test_reranker_provider_equivalence.py tests/unit/test_reranker.py tests/unit/test_reranker_registry.py tests/unit/test_relevance_gate.py
git commit -m "refactor: govern Reranker Provider calls"
```

---

## Task 8: Put Image preview behind a host-only input guard

**Files:**

- Create: `src/hl_mem/security/image_input.py`
- Modify: `src/hl_mem/security/__init__.py`
- Modify: `src/hl_mem/ingest/image_describer.py`
- Modify: `src/hl_mem/components.py`
- Create: `tests/unit/test_image_input_guard.py`
- Create: `tests/unit/test_image_provider_preview.py`
- Modify: `tests/unit/test_image_evidence.py`

**Interfaces:**

- `ImageInputGuard(max_bytes, allow_file_uris, file_allow_roots, client=None, max_redirects=3).materialize(ImagePart) -> ValidatedImageInput` is the only path from untrusted ImagePart to a Provider adapter.
- Base64 is strictly decoded; file paths are resolved and confined to allow-roots; HTTPS is downloaded by the host with streaming byte limits and manual redirect validation.
- Every initial/redirect HTTPS host is DNS-resolved and rejects loopback, private, link-local, multicast, unspecified, and reserved addresses for both IPv4 and IPv6.
- Final media type must match allowed MIME, HTTP Content-Type when present, and file magic. The validated object contains bytes/MIME/hash only; it contains no URI or filesystem path.
- `GovernedImageDescriber` resolves only an experimental capability, passes `ValidatedImageInput` to the adapter, records one image plus response token usage, and reconstructs the public locator from the host-held original ImagePart.
- Image plugin configuration remains explicit via `image_describer.mode = "on"`; plugin docs and diagnostics label it experimental.

- [ ] **Step 1: Add failing redirect, rebinding, file-root, MIME, and adapter-isolation tests**

```python
def test_redirect_to_private_address_is_rejected_before_second_fetch() -> None:
    guard = _guard(responses=[_redirect("https://127.0.0.1/private.png")])
    with pytest.raises(ImageInputError, match="private|loopback"):
        guard.materialize(_https_image())


def test_plugin_receives_materialized_bytes_not_source_locator(runtime: ProviderRuntime) -> None:
    _describe_with_recording_plugin(runtime, _https_image("https://public.test/a.png"))
    invocation = _recorded_invocation()
    assert invocation.image.data.startswith(b"\x89PNG")
    assert not hasattr(invocation.image, "uri")
```

Also test oversized chunked response, redirect limit, hostname resolving to mixed public/private IPs, file symlink escape, extension/MIME/magic disagreement, base64 bounds, and zero network usage on guard rejection.

- [ ] **Step 2: Run tests and observe current URI pass-through**

```powershell
uv run --frozen python -m pytest tests/unit/test_image_input_guard.py tests/unit/test_image_provider_preview.py tests/unit/test_image_evidence.py -q --tb=short
```

- [ ] **Step 3: Implement materialization and the experimental governed proxy**

The guard may use httpx only for image acquisition; Provider plugin code receives no acquisition client. Keep caption/OCR/confidence bounds and host-side locator construction identical to the existing behavior.

- [ ] **Step 4: Run image ingest, request-limit, and secret-redaction regressions**

```powershell
uv run --frozen python -m pytest tests/unit/test_image_input_guard.py tests/unit/test_image_provider_preview.py tests/unit/test_image_evidence.py tests/unit/test_http_utils.py tests/integration/test_extract_pipeline.py -q --tb=short
```

- [ ] **Step 5: Commit the experimental Image boundary**

```powershell
git add src/hl_mem/security src/hl_mem/ingest/image_describer.py src/hl_mem/components.py tests/unit/test_image_input_guard.py tests/unit/test_image_provider_preview.py tests/unit/test_image_evidence.py
git commit -m "feat: isolate experimental Image Providers"
```

---

## Task 9: Freeze plugin contracts, diagnostics, packaging, and Phase 3 gates

**Files:**

- Create: `scripts/check_provider_plugin_api.py`
- Create: `docs/provider-plugin-api.json`
- Create: `docs/provider-plugins.md`
- Create: `tests/fixtures/provider_plugin/pyproject.toml`
- Create: `tests/fixtures/provider_plugin/src/hl_mem_test_provider/__init__.py`
- Modify: `src/hl_mem/doctor.py`
- Modify: `src/hl_mem/application/health.py`
- Modify: `src/hl_mem/api/server.py`
- Modify: `src/hl_mem/mcp/server.py`
- Modify: `src/hl_mem/workers/worker.py`
- Modify: `scripts/check_docs_consistency.py`
- Modify: `scripts/check_imports.py`
- Modify: `scripts/complexity_budget.json`
- Modify: `.github/workflows/test.yml`
- Modify: `README.md`
- Modify: `README_EN.md`
- Modify: `docs/architecture.md`
- Modify: `docs/capability-matrix.md`
- Modify: `docs/configuration.md`
- Modify: `docs/compatibility.md`
- Modify: `docs/CHANGELOG.md`
- Modify: `AGENTS.md`
- Modify: `config.example.toml`
- Modify: `tests/unit/test_doctor.py`
- Create: `tests/integration/test_provider_plugin_wheel.py`
- Create: `tests/integration/test_provider_runtime_coverage.py`

**Interfaces:**

- `scripts/check_provider_plugin_api.py` freezes Entry Point group, API major, public exports, enums, dataclass fields/defaults, manifest fields, and stable Protocol method signatures. Experimental Image types are recorded in a separately labeled section.
- `doctor` reports stable codes for plugin discovery/compatibility/conflicts/config, trusted-in-process warning, usage sidecar schema/readability, expired recovery counts in read-only preview form, and enabled Provider resolution. It does not load disabled entry points or create/modify the sidecar.
- `/healthz` reports only plugin IDs, capability/name, stability, health state, and usage aggregates; no plugin config, endpoint, model response, or secret value.
- Runtime integration test instruments `httpx` and proves every enabled LLM, Embedding batch, Reranker, and Image Provider request has one finalized usage record; all disabled/Fake paths have zero records.
- Clean-wheel integration installs the built HL-Mem wheel and the fixture plugin into a fresh environment, enables it in v1 config, resolves one stable capability, and proves a manifest conflict prevents server construction before network traffic.
- CI runs config/OpenAPI/MCP/Provider-API snapshots together and adds the clean-wheel plugin test. No network service is contacted.

- [ ] **Step 1: Add failing API snapshot, doctor, runtime-coverage, and wheel tests**

```python
def test_every_actual_provider_request_has_one_final_usage_event(runtime_harness) -> None:
    runtime_harness.exercise_llm_embedding_reranker_and_image()
    assert runtime_harness.http_attempts == runtime_harness.finalized_usage_attempts
    assert runtime_harness.open_reservations == 0
```

Also assert disabled plugins are not imported by doctor, conflicts fail app/MCP/Worker construction, experimental labels are visible, and health output is secret-free.

- [ ] **Step 2: Run focused closure tests and observe missing diagnostics/snapshot/package proof**

```powershell
uv run --frozen --extra sqlite-vec python -m pytest tests/unit/test_doctor.py tests/integration/test_provider_runtime_coverage.py tests/integration/test_provider_plugin_wheel.py -q --tb=short
uv run --frozen python scripts/check_provider_plugin_api.py
```

- [ ] **Step 3: Implement diagnostics and generate stable snapshots/docs**

```powershell
uv run --frozen python scripts/check_provider_plugin_api.py --write
uv run --frozen python scripts/check_provider_plugin_api.py
uv run --frozen python scripts/check_config_schema_snapshot.py --write
uv run --frozen python scripts/generate_configuration_reference.py
```

Document exact trust, allowlist, configuration, secret, version, conflict, budget, recovery, and Image experimental boundaries. Do not market plugins as sandboxed.

- [ ] **Step 4: Run the complete Phase 3 gate**

```powershell
uv run --frozen --extra sqlite-vec python -m pytest tests/unit/test_provider_plugin_contracts.py tests/unit/test_provider_manifest.py tests/unit/test_provider_discovery.py tests/unit/test_provider_registry.py tests/unit/test_usage_governor.py tests/unit/test_provider_transport.py tests/unit/test_governed_provider_call.py tests/unit/test_llm_provider_equivalence.py tests/unit/test_embedding_provider_equivalence.py tests/unit/test_reranker_provider_equivalence.py tests/unit/test_image_input_guard.py tests/unit/test_image_provider_preview.py tests/unit/test_doctor.py tests/integration/test_provider_runtime_coverage.py tests/integration/test_provider_plugin_wheel.py -q --tb=short
uv run --frozen --extra sqlite-vec python -W error::ResourceWarning -m pytest tests/ -q --tb=short
uv run --frozen --extra sqlite-vec python -m ruff check src tests scripts
uv run --frozen --extra sqlite-vec python -m black --check .
uv run --frozen --extra sqlite-vec python -m isort --check-only .
uv run --frozen --extra sqlite-vec python -m mypy src/hl_mem --ignore-missing-imports
uv run --frozen --extra sqlite-vec python scripts/check_imports.py
uv run --frozen --extra sqlite-vec python scripts/check_complexity_budget.py --ratchet
uv run --frozen --extra sqlite-vec python scripts/check_docs_consistency.py
uv run --frozen --extra sqlite-vec python scripts/check_config_schema_snapshot.py
uv run --frozen --extra sqlite-vec python scripts/check_provider_plugin_api.py
uv run --frozen --extra sqlite-vec python scripts/check_openapi_snapshot.py
uv run --frozen --extra sqlite-vec python scripts/check_mcp_snapshot.py
uv build
```

- [ ] **Step 5: Review scope and commit Phase 3 closeout**

```powershell
git diff --check
git status --short
git add .github/workflows/test.yml AGENTS.md README.md README_EN.md config.example.toml docs scripts src/hl_mem/doctor.py src/hl_mem/application/health.py src/hl_mem/api/server.py src/hl_mem/mcp/server.py src/hl_mem/workers/worker.py tests/fixtures/provider_plugin tests/unit/test_doctor.py tests/integration/test_provider_plugin_wheel.py tests/integration/test_provider_runtime_coverage.py
git commit -m "docs: freeze the Provider Plugin API"
```

Confirm the branch contains no plugin route/job/migration/storage hook, no main-memory database migration, no automatic-task behavior change, no Graph backend, and no unrelated directory reorganization.

---

## Phase 3 Completion Record

- [ ] Stable public Provider contracts cover LLM, Embedding, and Reranker; Image is visibly experimental.
- [ ] Disabled distributions are never imported; enabled missing/duplicate/incompatible/conflicting plugins fail before traffic.
- [ ] Built-in and external adapters use one Registry and business code receives only host proxies.
- [ ] All four real network paths use host-owned timeout, retry, error normalization, redaction, metrics, audit, and atomic usage governance.
- [ ] Every actual retry attempt is durably accounted; no Embedding logical-call double counting exists.
- [ ] Expired unsent reservations release; sent/ambiguous reservations settle conservatively.
- [ ] Plugin TOML is non-secret, namespace-confined, schema-validated, and cannot override host security.
- [ ] Image redirects, DNS/IPs, local paths, MIME/magic, size, and materialization are host-validated before plugin code.
- [ ] Provider API/config/OpenAPI/MCP snapshots, doctor, clean-wheel external plugin, focused tests, full strict suite, formatting, typing, imports, complexity, docs, and build all pass.
- [ ] Only after this record is complete: author `2026-08-30-hl-mem-core-1-0-phase-4-automation.md` against merged Phase 3 code.
