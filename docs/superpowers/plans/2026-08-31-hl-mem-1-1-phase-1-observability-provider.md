# HL-Mem 1.1 Phase 1 Observability and Provider Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give operators one safe, read-only view of Provider cost, latency, failures, jobs, worker evidence, conflicts, and SQLite files, then prove the built-in real Provider path with a disposable budget-bounded smoke run.

**Architecture:** Add a read-only reporting layer beside the existing write-owning `UsageGovernor`; aggregate main-database state through explicit SQL readers and render one versioned report through the CLI. Keep `/healthz` cheap with a separate current-day summary. Put live Provider verification under repository-only `benchmarks/provider/`, using temporary configuration/databases and the same production component assembly.

**Tech Stack:** Python 3.12-3.14, SQLite URI read-only mode, dataclasses/TypedDict, argparse, JSON Schema, existing `UsageGovernor`, pytest, uv.

## Global Constraints

- Base commit is approved design `40e004d` on `develop/1.1`; implement in a short worktree branch based on that exact commit.
- This phase adds no main-memory or usage-ledger migration. Reporting must accept current usage schema version 1 and fail clearly on missing, corrupt, or newer ledgers.
- `ops report` is read-only: no `UsageGovernor` construction, `recover_expired`, job retry, migration, WAL checkpoint, database creation, or file rewrite.
- `--since` accepts only positive integer hours/days (`1h` through `720h`, `1d` through `30d`). Compute one UTC `[since, until]` window and use it consistently.
- JSON schema version is `1`; its keys and value types are contract-tested. Human output may improve, but it must be derived from the same report object.
- A separate CLI process cannot prove an idle in-process worker is alive. Report process-local runtime when supplied by `/healthz`; otherwise report database-observed job heartbeat and use `unknown`, never a false `healthy`, when no heartbeat exists.
- Do not include prompts, queries, Claims, responses, request URLs, credentials, plugin options, raw job payloads, or raw errors. Error output is bounded to stable categories and counts.
- `/healthz` performs only current-day indexed aggregates; 30-day groupings and percentiles remain CLI-only.
- Provider cost is unknown unless the operator configures a versioned `usage.price_book_path`. With a price book, the host—not the plugin—prices conservative reservations and actual settlement. Without one, reports stay honestly unknown and any active money limit fails closed.
- Live smoke uses a disposable directory, database, usage sidecar, and namespace. It never reads or writes the production database or its `.env` file.
- Per run: at most 10 LLM requests, 30 embedding items, 100 rerank documents, and CNY 20 estimated cost. The release-cycle cumulative evidence ceiling is CNY 50.
- Each task is one commit and follows TDD. Preserve the existing RC service and do not restart or modify it from this phase worktree.

---

## Task 1: Define Time Windows and a Read-Only Usage Report

**Files:**

- Create: `src/hl_mem/observability/ops_report.py`
- Modify: `src/hl_mem/observability/__init__.py`
- Modify: `src/hl_mem/errors.py`
- Reuse: `src/hl_mem/observability/usage_types.py`
- Reuse: `src/hl_mem/observability/usage.py`
- Create: `tests/unit/test_ops_usage_report.py`

**Interfaces:**

```python
@dataclass(frozen=True, slots=True)
class ReportWindow:
    since: datetime
    until: datetime

def parse_report_window(value: str, *, now: datetime) -> ReportWindow: ...

class UsageLedgerReader:
    def __init__(self, path: Path) -> None: ...
    def report(self, window: ReportWindow, *, limits: UsageLimits) -> dict[str, object]: ...
    def health_summary(self, *, day: date, limits: UsageLimits, now: datetime) -> dict[str, object]: ...
```

- Open existing ledgers with `sqlite3.connect(f"file:{quoted_path}?mode=ro", uri=True)` and `PRAGMA query_only=ON`; never create parent directories.
- `report()` groups finalized events by `(capability, plugin_id, provider, model, status)` and returns request/token/item/document/image/cost totals, successes, errors, unknown outcomes/cost, last failure time, and deterministic P50/P95 latency.
- Reservation summary returns active and expired counts plus reserved units. It does not alter reservation state.
- Budget utilization is `None` for unlimited or unknowable cost; otherwise a decimal ratio in `[0, +inf)` without clamping overruns.
- Empty, missing, corrupt, and newer-schema states are distinct: empty valid ledger returns zeros; missing/corrupt/newer raises `OpsReportError` with a safe bounded message.

- [ ] **Step 1: Write failing parser and read-only ledger tests**

```python
def test_parse_report_window_rejects_unbounded_or_fractional_values() -> None:
    for value in ("0h", "1.5h", "31d", "720d", "day", "-1h"):
        with pytest.raises(ValueError, match="--since"):
            parse_report_window(value, now=NOW)


def test_reader_does_not_create_a_missing_ledger(tmp_path: Path) -> None:
    path = tmp_path / "missing.budget.db"
    with pytest.raises(OpsReportError, match="does not exist"):
        UsageLedgerReader(path).report(WINDOW, limits=UsageLimits())
    assert not path.exists()
```

Also seed settled success/error/unknown events and active/expired reservations. Assert exact grouping, UTC boundary inclusion, P50/P95, unknown-cost behavior, and that file size/mtime and `PRAGMA user_version` are unchanged.

- [ ] **Step 2: Run the focused tests and observe missing report types**

```powershell
uv run --frozen python -m pytest tests/unit/test_ops_usage_report.py -q --tb=short
```

Expected: collection fails because `hl_mem.observability.ops_report` does not exist.

- [ ] **Step 3: Implement bounded window parsing and read-only SQL aggregation**

Use integer micro-units throughout. Compute percentiles from stable `(latency_ms, id)` ordering with the nearest-rank rule documented in the module; do not use a random/sample approximation.

- [ ] **Step 4: Verify accounting compatibility and type safety**

```powershell
uv run --frozen python -m pytest tests/unit/test_ops_usage_report.py tests/unit/test_usage_governor.py -q --tb=short
uv run --frozen python -m mypy src/hl_mem/observability --ignore-missing-imports
```

- [ ] **Step 5: Commit the read-only usage reader**

```powershell
git add src/hl_mem/observability/ops_report.py src/hl_mem/observability/__init__.py src/hl_mem/errors.py tests/unit/test_ops_usage_report.py
git commit -m "feat: add read-only Provider usage reports"
```

---

## Task 2: Assemble Main-Database and File Health into a Versioned Ops Report

**Files:**

- Modify: `src/hl_mem/observability/ops_report.py`
- Modify: `src/hl_mem/storage/jobs.py`
- Reuse: `src/hl_mem/application/health.py`
- Reuse: `src/hl_mem/application/recall.py`
- Create: `tests/unit/test_ops_report.py`

**Interfaces:**

```python
OPS_REPORT_SCHEMA_VERSION: Final[int] = 1

def build_ops_report(
    connection: sqlite3.Connection,
    *,
    database_path: Path,
    usage_path: Path,
    settings: Settings,
    window: ReportWindow,
    now: datetime,
    worker_runtime: Mapping[str, object] | None = None,
) -> dict[str, object]: ...
```

- Add `JobRepository.report_snapshot(window, *, now, lease_seconds)`: counts by status and job type, failed/dead counts, oldest pending age, expired running leases, last safe failure category, latest stored heartbeat, and recall-side-effect backlog. Do not return payload or `last_error` text.
- Worker output uses `source="process"` when a supplied runtime snapshot has timestamps; otherwise `source="job_heartbeat"`; if neither exists, `state="unknown"` and `heartbeat_at=None`.
- File output covers main DB, `-wal`, `-shm`, and usage sidecar. Missing optional sidecars have size 0; missing main DB is an error.
- Warning codes are stable strings: `budget_near_limit`, `unknown_usage`, `expired_reservation`, `failed_jobs`, `stale_running_jobs`, `large_wal`, `worker_inactive`, and `worker_unknown`.
- Emit `worker_inactive` only when a trustworthy timestamp is older than two configured poll intervals. Emit `worker_unknown` otherwise; never call it healthy.
- WAL warning is `wal_size > max(database_size, 256 * 1024 * 1024)`.

- [ ] **Step 1: Add failing deterministic report tests**

```python
def test_report_warns_without_claim_or_error_text(tmp_path: Path) -> None:
    report = build_ops_report(_seeded_connection(tmp_path), **_inputs(tmp_path))
    encoded = json.dumps(report, ensure_ascii=False)
    assert report["schema_version"] == 1
    assert "failed_jobs" in report["warnings"]
    assert "private claim text" not in encoded
    assert "provider raw error" not in encoded
```

Cover no data, failed/dead jobs, expired lease, process heartbeat, database heartbeat, no heartbeat, conflict backlog, side-effect backlog, WAL threshold, missing SHM, and byte-for-byte stable key structure.

- [ ] **Step 2: Run tests and observe absent main report assembly**

```powershell
uv run --frozen python -m pytest tests/unit/test_ops_report.py -q --tb=short
```

- [ ] **Step 3: Implement one read transaction and filesystem metadata collection**

Use the supplied read-only connection. Job and conflict aggregates run inside one explicit read transaction. File sizes come from `Path.stat()` only; do not open WAL/SHM files.

- [ ] **Step 4: Run job, conflict, and recall-side-effect regressions**

```powershell
uv run --frozen python -m pytest tests/unit/test_ops_report.py tests/unit/test_job_progress.py tests/unit/test_api_observability.py tests/unit/test_p1_9_recall_side_effects.py tests/unit/test_recall_side_effects_deferred.py -q --tb=short
```

- [ ] **Step 5: Commit report assembly**

```powershell
git add src/hl_mem/observability/ops_report.py src/hl_mem/storage/jobs.py tests/unit/test_ops_report.py
git commit -m "feat: aggregate HL-Mem operational health"
```

---

## Task 3: Add `hl-mem ops report` and the Cheap Health Summary

**Files:**

- Modify: `src/hl_mem/cli.py`
- Modify: `src/hl_mem/plugins/runtime.py`
- Modify: `src/hl_mem/api/server.py`
- Modify: `src/hl_mem/application/health.py`
- Create: `tests/unit/test_ops_cli.py`
- Modify: `tests/unit/test_healthcheck.py`
- Modify: `tests/unit/test_provider.py`
- Create: `docs/ops-report.schema.json`
- Create: `scripts/check_ops_report_schema.py`
- Modify: `.github/workflows/test.yml`

**Interfaces:**

- CLI forms are exactly `hl-mem ops report --since 24h` and `hl-mem ops report --since 7d --json`; default is `24h`.
- `--json` prints one compact, sorted JSON object. Human mode prints fixed sections `Summary`, `Providers`, `Jobs`, `Worker`, `Storage`, `Conflicts`, and `Warnings` from the same object.
- Exit 0 for a valid report even with warnings. Invalid arguments use argparse exit 2. Missing/corrupt/newer databases print one safe error and exit 1.
- `ProviderRuntime.usage_health_snapshot() -> dict[str, object] | None` returns current-day failures, stale reservations, and request/token/cost utilization only.
- `/healthz.provider_usage` retains existing keys and adds `health`; do not add historical percentiles or job scans to this route.
- `scripts/check_ops_report_schema.py` validates the committed JSON schema against a generated empty report and a seeded report, not against source strings.

- [ ] **Step 1: Write failing CLI, exit-code, and health tests**

```python
def test_ops_report_json_is_versioned_and_read_only(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    before = _database_fingerprint(tmp_path)
    main(["--config", str(_config(tmp_path)), "ops", "report", "--since", "24h", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert _database_fingerprint(tmp_path) == before
```

Also assert human sections, invalid `--since`, corrupt ledger exit 1, no secret/content leakage, health SQL query count, and unchanged existing health keys.

- [ ] **Step 2: Run tests and observe the missing command/runtime method**

```powershell
uv run --frozen python -m pytest tests/unit/test_ops_cli.py tests/unit/test_provider.py tests/unit/test_healthcheck.py -q --tb=short
```

- [ ] **Step 3: Wire the command before normal mutable database construction**

Open the main database by URI `mode=ro`, set `row_factory=sqlite3.Row` and `PRAGMA query_only=ON`, build/print the report, and close deterministically. Reuse configuration loading but never instantiate `Database` or `UsageGovernor` for this command.

- [ ] **Step 4: Implement and freeze the JSON contract**

```powershell
uv run --frozen python scripts/check_ops_report_schema.py --write
uv run --frozen python scripts/check_ops_report_schema.py
uv run --frozen python scripts/check_openapi_snapshot.py
```

Review the generated schema before staging it. It must reject unknown top-level fields and must contain no content-bearing field.

- [ ] **Step 5: Run CLI/API regressions and commit**

```powershell
uv run --frozen python -m pytest tests/unit/test_ops_cli.py tests/unit/test_healthcheck.py tests/unit/test_provider.py tests/unit/test_api_observability.py -q --tb=short
git add src/hl_mem/cli.py src/hl_mem/plugins/runtime.py src/hl_mem/api/server.py src/hl_mem/application/health.py tests/unit/test_ops_cli.py tests/unit/test_healthcheck.py tests/unit/test_provider.py docs/ops-report.schema.json scripts/check_ops_report_schema.py .github/workflows/test.yml
git commit -m "feat: add the operational report command"
```

---

## Task 4: Add Optional Host-Owned Usage Pricing

**Files:**

- Create: `src/hl_mem/observability/pricing.py`
- Modify: `src/hl_mem/observability/__init__.py`
- Modify: `src/hl_mem/plugins/proxies.py`
- Modify: `src/hl_mem/plugins/runtime.py`
- Modify: `src/hl_mem/config/models.py`
- Modify: `src/hl_mem/config/loader.py`
- Modify: `config.example.toml`
- Modify: `docs/configuration.md`
- Modify: `docs/config-schema.json`
- Create: `docs/usage-pricing.schema.json`
- Create: `tests/unit/test_usage_pricing.py`
- Modify: `tests/unit/test_governed_provider_call.py`
- Modify: `tests/unit/test_provider.py`
- Modify: `tests/unit/test_config_loader.py`

**Interfaces:**

```python
class UsageCostEstimator(Protocol):
    @property
    def fingerprint(self) -> str: ...
    def price(self, identity: UsageIdentity, amount: UsageAmount, *, phase: Literal["reserve", "settle"]) -> UsageAmount: ...

class UsagePriceBook(UsageCostEstimator):
    @classmethod
    def load(cls, path: Path) -> UsagePriceBook: ...
```

- Add optional schema-v1 setting `usage.price_book_path`; absent means monetary cost stays unknown. A configured missing/invalid book fails doctor/startup clearly before traffic.
- Settings/health/audit expose only `price_book_configured` and the validated fingerprint, never the local price-book path or source URL list.
- Price-book schema version is 1, currency is exactly `CNY`, and rules key exact `(capability, model)` plus optional exact provider. No regex, remote include, expression, or code execution is allowed.
- Rates are non-negative integer microunits per request, million input/output tokens, embedding item, rerank document, or image. One CNY is 1,000,000 microunits.
- Reserve pricing uses conservative estimated units before `UsageGovernor.reserve`; settlement pricing uses actual measured units before `settle`. Retries remain priced per attempt by the existing governed-call algorithm.
- Missing matching rules return unknown cost. If `usage.daily_cost_limit_microunits > 0`, unknown reserve cost fails closed through the existing governor.
- `GovernedProviderCall` accepts an optional estimator supplied by `ProviderRuntime`; adapters/plugins never receive it. No estimator preserves current request/token behavior exactly.
- Metrics/audit include only the integer cost already recorded plus the non-secret price-book fingerprint; no source URL or file path enters runtime events.

- [ ] **Step 1: Write failing schema, arithmetic, and governance tests**

```python
def test_price_book_prices_reservation_and_actual_tokens_without_float_rounding() -> None:
    book = UsagePriceBook.load(PRICE_BOOK)
    reserved = book.price(LLM_IDENTITY, UsageAmount(requests=1, input_tokens=1_000, output_tokens=2_000), phase="reserve")
    assert reserved.cost_microunits == EXPECTED_RESERVE_COST


def test_active_money_limit_rejects_an_unpriced_model() -> None:
    with pytest.raises(UsageLimitExceededError, match="cost"):
        _governed(price_book=BOOK_WITHOUT_MODEL, cost_limit=1_000_000).execute_factory(
            lambda: REQUEST,
            ESTIMATE,
            parse_success,
            max_attempts=1,
        )
```

Also cover exact-provider precedence, duplicate/conflicting rule rejection, invalid currency/schema/rates, deterministic fingerprint, per-unit rounding upward, retry pricing, actual settlement, missing file, no-estimator equivalence, and secret-safe repr/audit.

- [ ] **Step 2: Run tests and observe absent pricing/config behavior**

```powershell
uv run --frozen python -m pytest tests/unit/test_usage_pricing.py tests/unit/test_governed_provider_call.py tests/unit/test_config_loader.py -q --tb=short
```

- [ ] **Step 3: Implement exact integer pricing at the host proxy boundary**

Use ceiling integer arithmetic for each positive billable unit. Preserve `UsageAmount` counters and replace only `cost_microunits`. Load and validate the book once when creating `ProviderRuntime`; never reopen it per request.

- [ ] **Step 4: Regenerate configuration docs and run compatibility checks**

```powershell
uv run --frozen python scripts/check_config_schema_snapshot.py --write
uv run --frozen python scripts/generate_configuration_reference.py
uv run --frozen python -m pytest tests/unit/test_usage_pricing.py tests/unit/test_governed_provider_call.py tests/unit/test_provider.py tests/unit/test_config_loader.py tests/unit/test_config_migrate.py -q --tb=short
uv run --frozen python scripts/check_config_schema_snapshot.py
```

Review generated diffs: the only new public config is optional `usage.price_book_path`; existing schema-v1 files remain valid.

- [ ] **Step 5: Commit host pricing separately from the smoke harness**

```powershell
git add src/hl_mem/observability/pricing.py src/hl_mem/observability/__init__.py src/hl_mem/plugins/proxies.py src/hl_mem/plugins/runtime.py src/hl_mem/config/models.py src/hl_mem/config/loader.py config.example.toml docs/configuration.md docs/config-schema.json docs/usage-pricing.schema.json tests/unit/test_usage_pricing.py tests/unit/test_governed_provider_call.py tests/unit/test_provider.py tests/unit/test_config_loader.py
git commit -m "feat: add optional Provider cost pricing"
```

---

## Task 5: Build a Disposable, Budget-Bounded Provider Live Smoke

**Files:**

- Create: `benchmarks/provider/README.md`
- Create: `benchmarks/provider/fixture.json`
- Create: `benchmarks/provider/live_smoke.py`
- Create: `benchmarks/provider/result_schema.json`
- Create: `tests/unit/test_provider_live_smoke.py`
- Modify: `.gitignore`
- Modify: `scripts/check_wheel_contents.py`

**Interfaces:**

```python
@dataclass(frozen=True, slots=True)
class LiveSmokeLimits:
    llm_requests: int = 10
    embedding_items: int = 30
    rerank_documents: int = 100
    cost_microunits: int = 20_000_000

def run_live_smoke(config: Path, output: Path, *, limits: LiveSmokeLimits) -> dict[str, object]: ...
```

- The script requires an explicit config path and output path. It rejects a database path outside its newly created temporary root and rejects Fake components.
- Fixture text is synthetic Chinese/English product data committed to the repository. The result stores only fixture SHA-256, configuration fingerprint, package/plugin/model labels, counters, latency, safe error categories, and pass/fail checks.
- It exercises ingest/extract, Claim/Evidence/entity/vector persistence, ordinary/entity/temporal/preference recall, reranker success and controlled failure fallback, usage settlement, and resource closure.
- The live harness requires a versioned price-book JSON accepted by Task 4, records its SHA/effective date/source URLs, and sets the temporary config's `usage.price_book_path`; plugin adapters never see pricing.
- It preflights configured limits before the first call and checks the final ledger against them. Missing model rules or unknown cost under an active money limit fails closed.
- Unit tests use recording adapters, not paid services. The script is never part of normal CI and is absent from the wheel.

- [ ] **Step 1: Write failing harness-safety tests**

```python
def test_live_smoke_refuses_production_database_path(tmp_path: Path) -> None:
    with pytest.raises(LiveSmokeSafetyError, match="temporary root"):
        run_live_smoke(_config(database="D:/production/hl_mem.db"), tmp_path / "result.json", limits=LIMITS)


def test_result_contains_no_fixture_or_provider_content(tmp_path: Path) -> None:
    result = _run_with_recording_providers(tmp_path)
    encoded = json.dumps(result, ensure_ascii=False)
    assert FIXTURE_TEXT not in encoded
    assert "Bearer" not in encoded
```

Also test request/item/document/cost overrun, unknown cost, Fake rejection, zero active reservations, reranker fallback, temp cleanup, and result-schema validation.

- [ ] **Step 2: Run tests and observe the absent harness**

```powershell
uv run --frozen python -m pytest tests/unit/test_provider_live_smoke.py -q --tb=short
```

- [ ] **Step 3: Implement the harness through production composition roots**

Use `load_settings`, `create_provider_runtime`, `make_embedder`, `make_reranker`, `LLMExtractor`, `IngestService`, and `RecallService`; do not duplicate HTTP clients or bypass governed proxies. Close every service in `finally` blocks.

- [ ] **Step 4: Verify artifact and packaging boundaries**

```powershell
uv run --frozen python -m pytest tests/unit/test_provider_live_smoke.py -q --tb=short
uv run --frozen python -m build
$wheel = Get-ChildItem dist/*.whl | Sort-Object LastWriteTime -Descending | Select-Object -First 1
uv run --frozen python scripts/check_wheel_contents.py $wheel.FullName
```

Expected: the wheel contains no `benchmarks/provider`, result JSON, temporary database, or external plugin code.

- [ ] **Step 5: Commit the explicit live harness**

```powershell
git add benchmarks/provider tests/unit/test_provider_live_smoke.py .gitignore scripts/check_wheel_contents.py
git commit -m "test: add governed Provider live smoke"
```

---

## Task 6: Run Built-In Providers and Close Phase 1

**Files:**

- Create after successful run: `benchmarks/provider/results/1.1.0-builtin-summary.json`
- Modify: `benchmarks/provider/README.md`
- Modify: `docs/CHANGELOG.md`

**Interfaces:**

- The committed summary uses `result_schema.json`, contains `provider_kind="builtin"`, exact core commit, UTC run time, fixture/config fingerprints, model labels, aggregate usage/latency/error data, zero active reservations, and check outcomes.
- It contains no config path, environment-variable value, endpoint, request/response content, or temporary path.

- [ ] **Step 1: Preflight the disposable paths, credentials, and remaining evidence budget**

Run `hl-mem doctor` against a temporary config that points only to a new temporary directory. Inspect the cumulative committed summaries and prove the requested run remains within CNY 50 before making a call. Build a run-scoped price book from current official Provider pricing, validate its schema and model coverage, and record its effective date/source URLs/hash; because pricing is time-sensitive, verify the official source at execution time. Never read the production `.env` implicitly; pass a dedicated env file path.

- [ ] **Step 2: Run the built-in live smoke once**

```powershell
$smokeRoot = Join-Path ([IO.Path]::GetTempPath()) "hl-mem-1.1-builtin-smoke"
$smokeConfig = Join-Path $smokeRoot "hl_mem.toml"
$smokeEnv = "D:\workspace\hl_agent\hl_mem\var\provider-live.env"
$priceBook = Join-Path $smokeRoot "pricing.json"
uv run --frozen python benchmarks/provider/live_smoke.py --config $smokeConfig --env-file $smokeEnv --price-book $priceBook --output benchmarks/provider/results/1.1.0-builtin-summary.json
```

Expected: all checks pass, total usage stays inside the per-run limits, and active reservations are zero. On failure, preserve only the sanitized failed artifact, diagnose before retrying, and count every attempt toward the CNY 50 cycle budget.

- [ ] **Step 3: Scan the result and repository for leaked material**

Run the existing secret scanner plus a focused scan for the dedicated credentials and fixture text. The scan must inspect the produced artifact and git diff, not merely filenames.

- [ ] **Step 4: Run the Phase 1 gate**

```powershell
uv run --frozen python -m pytest tests/unit/test_ops_usage_report.py tests/unit/test_ops_report.py tests/unit/test_ops_cli.py tests/unit/test_usage_pricing.py tests/unit/test_provider_live_smoke.py -q --tb=short
uv run --frozen python -m pytest tests/unit/ -q --tb=short
uv run --frozen python -m ruff check .
uv run --frozen python -m black --check .
uv run --frozen python -m isort --check-only .
uv run --frozen python -m mypy src/hl_mem/ --ignore-missing-imports
uv run --frozen python scripts/check_imports.py
uv run --frozen python scripts/check_complexity_budget.py --ratchet
uv run --frozen python scripts/check_config_schema_snapshot.py
uv run --frozen python scripts/check_ops_report_schema.py
uv run --frozen python scripts/check_openapi_snapshot.py
uv run --frozen python -m build
```

- [ ] **Step 5: Commit sanitized evidence and Phase 1 documentation**

```powershell
git add benchmarks/provider/results/1.1.0-builtin-summary.json benchmarks/provider/README.md docs/CHANGELOG.md
git commit -m "docs: record built-in Provider evidence"
```

Do not commit a result until the leak scan and schema check pass. Do not publish or deploy from this task.
