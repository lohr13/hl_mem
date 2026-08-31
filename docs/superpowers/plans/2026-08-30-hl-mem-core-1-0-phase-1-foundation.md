# HL-Mem Core 1.0 Phase 1 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish the Core 1.0 correctness and safety foundation: a truthful Python support matrix, deterministic repository text rules, byte-accurate request limits, temporally correct historical recall, explicit SQLite ownership, zero `ResourceWarning` during the pytest-managed lifecycle, and a written 1.x compatibility contract.

**Architecture:** Preserve the existing modular monolith and public REST/MCP schemas. Add one transport-only ASGI middleware, one small recall policy predicate, context-manager ownership to `Database`, and test fixtures that own test-created resources. This phase changes no memory schema and adds no new product capability.

**Tech Stack:** Python 3.12-3.14, FastAPI/Starlette ASGI, SQLite, Pydantic, uv, pytest, mypy, Ruff, Black, isort, GitHub Actions.

## Global Constraints

- Baseline is `v0.36.1 / 2dbb6a9`; approved design is commit `7619d7c`.
- Do not reorganize the repository, change OpenAPI/MCP schemas, add migrations, or begin Provider/plugin work in this phase.
- Do not delete the user's untracked `.coverage`, `Temp/`, `nul`, backup, or research files; only add ignore rules for future artifacts.
- Do not suppress `ResourceWarning`. Every supported connection path must have an owner, and CI must promote warnings observed during pytest execution to errors. Warnings emitted only after pytest hooks have ended during interpreter finalization are recorded for the later“SQLite 连接生命周期观测”专项 and are not a Phase 1 release gate.
- Historical requests containing either `as_of` or `known_as_of` must omit Policy and Derivation context until those models have real bitemporal versions.
- Request limiting must count ASGI body bytes and retain at most `max_request_body`; `Content-Length` is only an early rejection hint.
- Each task ends in a focused commit. Run the phase gate only after all task commits pass individually.

---

## Task 1: Make Python support and repository text rules truthful

**Consumes:** Current packaging metadata, documentation, CI workflow, and local artifact conventions.

**Produces:** One consistent Python 3.12-3.14 support statement, LF rules for source/config/docs, and ignore rules that prevent known workspace artifacts from reappearing.

**Files:**

- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `.github/workflows/test.yml`
- Modify: `.gitattributes`
- Modify: `.gitignore`
- Modify: `README.md`
- Modify: `README_EN.md`
- Modify: `AGENTS.md`

- [ ] **Step 1: Capture the current published metadata and configuration baseline**

This task changes packaging, CI, documentation, and repository configuration only. The user approved the TDD exception: do not add pytest tests that merely inspect source text.

Run `uv build`, then read the built wheel's `METADATA` with `zipfile` and record the current `Requires-Python` and Python classifiers. Record the current `git check-attr eol` results for one Python, Markdown, TOML, and YAML file. These commands provide a real artifact baseline instead of source-string assertions.

- [ ] **Step 2: Apply the minimal metadata and documentation changes**

- Set `project.requires-python = ">=3.12"`.
- Add Python classifiers for 3.12, 3.13, and 3.14.
- Set Black target to `py312`.
- In the `test`, `build`, and `migrations` jobs use `python-version: ["3.12", "3.13", "3.14"]`; keep lint, format, type, and documentation jobs on one canonical version to avoid duplicating non-runtime work.
- Change the Python badges and installation text in both READMEs and `AGENTS.md` to 3.12+.
- Add LF rules for Python, Markdown, TOML, and YAML without rewriting unrelated files.
- Add root-scoped ignores for `.coverage`, `Temp/`, `nul`, and `hl_mem.toml.bak_*`; do not remove existing files.
- Regenerate the lock with `uv lock`; do not hand-edit dependency resolutions.

- [ ] **Step 3: Verify built metadata, lock, text attributes, and documentation**

Run:

```powershell
uv build
uv lock --check
& '.\.venv\Scripts\python.exe' -c "from pathlib import Path; import zipfile; wheel=next(Path('dist').glob('*.whl')); z=zipfile.ZipFile(wheel); name=next(n for n in z.namelist() if n.endswith('.dist-info/METADATA')); text=z.read(name).decode(); assert 'Requires-Python: >=3.12' in text; assert all(f'Programming Language :: Python :: {v}' in text for v in ('3.12','3.13','3.14'))"
git check-attr eol -- src/hl_mem/__init__.py README.md pyproject.toml .github/workflows/test.yml
& '.\.venv\Scripts\python.exe' scripts/check_docs_consistency.py
```

Expected: all commands PASS.

- [ ] **Step 4: Commit the task**

```powershell
git add pyproject.toml uv.lock .github/workflows/test.yml .gitattributes .gitignore README.md README_EN.md AGENTS.md
git commit -m "build: align Python support and repository rules"
```

---

## Task 2: Enforce request size from actual ASGI bytes

**Consumes:** `settings.max_request_body` and the current FastAPI application assembly.

**Produces:** A transport-only `RequestSizeLimitMiddleware` that cannot be bypassed with a missing, false, streamed, or malformed `Content-Length` header.

**Files:**

- Create: `src/hl_mem/api/request_limits.py`
- Create: `tests/unit/test_request_size_limit.py`
- Modify: `src/hl_mem/api/server.py`

- [ ] **Step 1: Add failing raw-ASGI tests**

Create a small ASGI harness that supplies a list of `http.request` messages and captures `http.response.start` / `http.response.body`. Test these exact cases:

```python
@pytest.mark.asyncio
async def test_missing_content_length_cannot_bypass_limit() -> None:
    response = await invoke([b"1234", b"5678", b"9"], headers=[], limit=8)
    assert response.status == 413
    assert response.downstream_calls == 0


@pytest.mark.asyncio
async def test_false_small_content_length_cannot_bypass_limit() -> None:
    response = await invoke([b"12345", b"67890"], headers=[(b"content-length", b"2")], limit=8)
    assert response.status == 413
    assert response.downstream_calls == 0


@pytest.mark.asyncio
async def test_body_at_limit_is_replayed_without_change() -> None:
    response = await invoke([b"123", b"45678"], headers=[], limit=8)
    assert response.status == 200
    assert response.downstream_body == b"12345678"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [b"abc", b"-1", b"3,4"])
async def test_malformed_content_length_is_bad_request(value: bytes) -> None:
    response = await invoke([b"{}"], headers=[(b"content-length", value)], limit=8)
    assert response.status == 400
```

Also test declared length `limit + 1` returns 413 before reading, empty bodies pass, and `http.disconnect` is replayed correctly.

- [ ] **Step 2: Run the tests and confirm the current middleware fails the transport contract**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/unit/test_request_size_limit.py -v
```

Expected: FAIL because the new module does not exist and the current `BaseHTTPMiddleware` checks only the declared length.

- [ ] **Step 3: Implement bounded raw-ASGI buffering and replay**

Implement `RequestSizeLimitMiddleware` as a plain ASGI callable:

```python
class RequestSizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_request_body: int) -> None:
        if max_request_body < 0:
            raise ValueError("max_request_body must be non-negative")
        self.app = app
        self.max_request_body = max_request_body

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = parse_content_length(scope.get("headers", []))
        if declared is INVALID:
            await send_plain_response(send, 400, b"Invalid Content-Length")
            return
        if declared is not None and declared > self.max_request_body:
            await send_plain_response(send, 413, b"Request body too large")
            return

        messages: list[Message] = []
        retained = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                messages.append(message)
                break
            if message["type"] != "http.request":
                continue
            body = message.get("body", b"")
            if len(body) > self.max_request_body - retained:
                await send_plain_response(send, 413, b"Request body too large")
                return
            retained += len(body)
            messages.append(message)
            if not message.get("more_body", False):
                break

        await self.app(scope, replay(messages), send)
```

Implementation rules:

- Parse all `Content-Length` headers; duplicate unequal values are invalid.
- Retain at most `max_request_body` bytes. Inspect each received chunk before appending it to the replay buffer; an oversized chunk is transient and is never retained by the middleware.
- Preserve message boundaries and `more_body` during replay.
- Do not import application services, settings, or storage in `request_limits.py`.
- Remove the old `RequestSizeLimitMiddleware` class from `server.py` and import the new class; leave registration at the existing `app.add_middleware(...)` point.

- [ ] **Step 4: Verify transport behavior and public contract stability**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/unit/test_request_size_limit.py tests/unit/test_api_observability.py -q --tb=short
& '.\.venv\Scripts\python.exe' scripts/check_openapi_snapshot.py
```

Expected: tests PASS and OpenAPI snapshot is unchanged.

- [ ] **Step 5: Commit the task**

```powershell
git add src/hl_mem/api/request_limits.py src/hl_mem/api/server.py tests/unit/test_request_size_limit.py
git commit -m "fix: enforce request limits on actual body bytes"
```

---

## Task 3: Keep current-only auxiliary memory out of historical recall

**Consumes:** `RecallRequest.as_of`, `RecallRequest.known_as_of`, current Policy matching, and current Derivation/Observation assembly.

**Produces:** One explicit temporal guard shared by standard recall assembly; current recall remains unchanged, while any historical request omits Policy and Derivation context.

**Files:**

- Create: `tests/unit/test_recall_historical_auxiliary.py`
- Modify: `src/hl_mem/application/recall.py`
- Modify: `tests/unit/test_recall_observation_stopgap.py`

- [ ] **Step 1: Add failing temporal-isolation tests**

Add a request-matrix unit test for the policy predicate and API regression tests using `create_app`:

```python
@pytest.mark.parametrize(
    ("as_of", "known_as_of", "expected"),
    [
        (None, None, True),
        ("2026-01-01T00:00:00Z", None, False),
        (None, "2026-01-01T00:00:00Z", False),
        ("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", False),
    ],
)
def test_auxiliary_context_requires_current_time(as_of, known_as_of, expected) -> None:
    assert auxiliary_context_is_current(as_of=as_of, known_as_of=known_as_of) is expected
```

For the API regression:

- Seed one active Policy whose trigger matches the query.
- Prove a current `/v1/recall` response can include it.
- Send the same query with `as_of`, then with only `known_as_of`.
- Assert both historical responses contain `policies == []` and `observations == []`.
- Assert Claim result filtering still uses the existing bitemporal path; do not weaken or duplicate Claim filtering.
- Update the stopgap test to close its database owner explicitly.

- [ ] **Step 2: Run the focused tests and observe current Policy leakage**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/unit/test_recall_historical_auxiliary.py tests/unit/test_recall_observation_stopgap.py -v
```

Expected: the historical Policy assertions FAIL because `_enrich_standard_results()` currently lists active policies for every request.

- [ ] **Step 3: Add one guard at the assembly boundary**

Implement a pure helper near `RecallRequest`:

```python
def auxiliary_context_is_current(*, as_of: str | None, known_as_of: str | None) -> bool:
    return as_of is None and known_as_of is None
```

Use it only inside `_enrich_standard_results()`:

```python
if auxiliary_context_is_current(as_of=request.as_of, known_as_of=request.known_as_of):
    observations = self._assemble_observations([claim["id"] for claim in selection.claims])
    policies = matching_policies(
        ExperienceService(self.connection).list_policies("active", namespace=request.namespace),
        request.query,
    )
    # Existing evidence attachment remains here.
else:
    observations = []
    policies = []
```

Do not invent historical Policy/Derivation timestamps, mutate their schema, or change REST/MCP fields.

- [ ] **Step 4: Verify REST, MCP, and snapshot behavior**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/unit/test_recall_historical_auxiliary.py tests/unit/test_recall_observation_stopgap.py tests/unit/test_recall_context.py -q --tb=short
& '.\.venv\Scripts\python.exe' scripts/check_openapi_snapshot.py
& '.\.venv\Scripts\python.exe' scripts/check_mcp_snapshot.py
```

Expected: all tests and both snapshots PASS unchanged.

- [ ] **Step 5: Commit the task**

```powershell
git add src/hl_mem/application/recall.py tests/unit/test_recall_historical_auxiliary.py tests/unit/test_recall_observation_stopgap.py
git commit -m "fix: isolate historical recall from current-only context"
```

---

## Task 4: Establish explicit database ownership APIs and test fixtures

**Consumes:** `Database.open()`, `connect()`, `connect_readonly()`, `close()`, and the current pytest environment fixture.

**Produces:** Context-manager ownership for `Database`, a pytest-level SQLite owner that deterministically closes test-created resources, and lifecycle tests that can opt out of automatic cleanup to verify production owners honestly.

**Files:**

- Create: `tests/support/__init__.py`
- Create: `tests/support/sqlite_ownership.py`
- Create: `tests/unit/test_database_lifecycle.py`
- Modify: `src/hl_mem/storage/database.py`
- Modify: `tests/conftest.py`

- [ ] **Step 1: Add failing lifecycle tests**

```python
def test_database_context_closes_every_owned_connection(tmp_path) -> None:
    with Database(tmp_path / "owned.db") as database:
        direct = database.open()
        worker = database.open_worker()
        with database.connect() as pooled:
            pooled.execute("SELECT 1")

    for connection in (direct, worker, pooled):
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def test_database_close_is_idempotent(tmp_path) -> None:
    database = Database(tmp_path / "idempotent.db")
    database.open()
    database.close()
    database.close()
```

Add a fixture contract test that requests `database_factory` and `sqlite_connection_factory`, opens resources, and lets pytest teardown own them without emitting a warning.

- [ ] **Step 2: Run the tests and confirm the missing owner protocol**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -W error::ResourceWarning -m pytest tests/unit/test_database_lifecycle.py -v
```

Expected: FAIL because `Database` is not a context manager and the ownership fixtures do not exist.

- [ ] **Step 3: Add the minimal `Database` context-manager protocol**

Import `TracebackType` from `types`, then add:

```python
def __enter__(self) -> Database:
    return self


def __exit__(
    self,
    exc_type: type[BaseException] | None,
    exc: BaseException | None,
    traceback: TracebackType | None,
) -> None:
    self.close()
```

Keep `close()` idempotent. Do not change `open()` return type or pool behavior in this phase.

- [ ] **Step 4: Add explicit pytest resource owners**

Implement in `tests/support/sqlite_ownership.py`:

```python
class TestSQLiteOwner:
    def __init__(self) -> None:
        self.databases: list[Database] = []
        self.connections: list[sqlite3.Connection] = []

    def database(self, path: Path, *, settings: Settings | None = None, **kwargs: object) -> Database:
        database = Database(path, settings=settings, **kwargs)
        self.databases.append(database)
        return database

    def connect(self, *args: object, **kwargs: object) -> sqlite3.Connection:
        connection = sqlite3.connect(*args, **kwargs)
        self.connections.append(connection)
        return connection

    def close(self) -> None:
        for database in reversed(self.databases):
            database.close()
        for connection in reversed(self.connections):
            connection.close()
```

TestSQLiteOwner.install(monkeypatch) must retain the original Database.__init__ and sqlite3.connect, then wrap them to register every resource created during one test. Registration is idempotent by object identity, so resources created through the named factories are not registered twice. Hold strong references until teardown so CPython cannot emit a warning before the owner closes them.

Expose these function-scoped fixtures from `tests/conftest.py`:

- `database_factory`: returns `owner.database` and closes every registered `Database` after the test.
- `sqlite_connection_factory`: returns `owner.connect` and closes every registered raw connection after the test.
- `sqlite_test_owner` (autouse): installs the registration wrappers and closes registered `Database` owners first, then raw connections, in reverse creation order.

Register a `no_sqlite_autoclose` marker. When present, the autouse fixture must not install wrappers; lifecycle tests use this marker to prove API/MCP/Worker shutdown closes its own connection. Do not suppress warnings or add a destructor to `Database`.

- [ ] **Step 5: Verify the lifecycle contract**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -W error::ResourceWarning -m pytest tests/unit/test_database_lifecycle.py tests/unit/test_concurrency.py -q --tb=short
```

Expected: all tests PASS with no ResourceWarning.

- [ ] **Step 6: Commit the task**

```powershell
git add src/hl_mem/storage/database.py tests/conftest.py tests/support/__init__.py tests/support/sqlite_ownership.py tests/unit/test_database_lifecycle.py
git commit -m "test: establish explicit SQLite resource ownership"
```

---

## Task 5: Prove production lifecycle closure and install the warning gate

**Consumes:** Task 4 ownership fixtures and the measured baseline `2414 passed, 1 skipped, 541 ResourceWarning instances` when warnings are expanded.

**Produces:** Independent evidence that long-lived production owners close themselves, deterministic cleanup for isolated tests, and CI failure on unowned resources observable during the pytest-managed lifecycle.

**Files:**

- Modify: `.github/workflows/test.yml`
- Create: `tests/unit/test_runtime_resource_ownership.py`
- Modify: `tests/conftest.py`

- [ ] **Step 1: Freeze the measured warning baseline**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -W always::ResourceWarning -m pytest tests/unit -q --tb=no
```

Expected baseline before Task 4: `2414 passed, 1 skipped`, with 541 expanded `ResourceWarning` instances. After Task 4 the same test count must complete without a ResourceWarning summary.

- [ ] **Step 2: Add production-owner tests without automatic cleanup**

Mark every test in `test_runtime_resource_ownership.py` with `pytest.mark.no_sqlite_autoclose`. Add these exact proofs:

- FastAPI: hold the connection returned by `app.state.db.open()` inside `TestClient`; after the client lifespan exits, executing on that connection raises `sqlite3.ProgrammingError`.
- MCP: construct `McpMemoryServer`, hold its connection, call `close()`, and assert the connection is closed.
- Worker: construct `Worker`, hold its worker connection, call `close()`, and assert the connection is closed.
- `Database` context: rely on Task 4's test to cover direct and pooled connections.

Use `try/finally` inside each test so a failed assertion does not itself leak the resource being tested. These tests must not depend on garbage collection timing.

- [ ] **Step 3: Run the owner proofs with ResourceWarning as an error**

```powershell
& '.\.venv\Scripts\python.exe' -W error::ResourceWarning -m pytest tests/unit/test_runtime_resource_ownership.py tests/unit/test_database_lifecycle.py -v
```

Expected: PASS. If one named owner fails, fix only its existing shutdown hook in `src/hl_mem/api/server.py`, `src/hl_mem/mcp/server.py`, or `src/hl_mem/workers/worker.py`; do not add a global production connection registry.

- [ ] **Step 4: Verify deterministic ownership across all test suites**

Run each suite independently:

```powershell
& '.\.venv\Scripts\python.exe' -W error::ResourceWarning -m pytest tests/unit -q --tb=short
& '.\.venv\Scripts\python.exe' -W error::ResourceWarning -m pytest tests/integration -q --tb=short
& '.\.venv\Scripts\python.exe' -W error::ResourceWarning -m pytest tests/eval -q --tb=short
```

Expected: all three commands PASS; there is no warning summary or unraisable SQLite warning before pytest completes. A warning first emitted after all pytest hooks have ended during interpreter finalization is outside this gate and is tracked in `docs/research/sqlite-connection-lifecycle.md`.

- [ ] **Step 5: Promote ResourceWarning to an error in CI**

Add `-W error::ResourceWarning` to the full-suite pytest command in `.github/workflows/test.yml`. Do not add a global ignore or reduce test discovery.

- [ ] **Step 6: Verify the complete warning gate**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -W error::ResourceWarning -m pytest tests/ -q --tb=short
```

Expected: full suite PASS, zero ResourceWarning.

- [ ] **Step 7: Commit the task**

Stage only the production/test ownership changes reported by `git status`; preserve unrelated untracked files.

```powershell
git add .github/workflows/test.yml tests/conftest.py tests/unit/test_runtime_resource_ownership.py
git commit -m "test: enforce deterministic SQLite cleanup"
```

---

## Task 6: Publish the 1.x compatibility policy and close the phase

**Consumes:** The approved design's stable/Beta/experimental classifications and the existing 0.x-only compatibility policy.

**Produces:** A non-ambiguous 1.x contract, automated policy checks, and a complete Phase 1 verification record.

**Files:**

- Modify: `docs/compatibility.md`

- [ ] **Step 1: Confirm the current policy scope**

Read `docs/compatibility.md` and confirm it explicitly applies only to 0.x. The user approved the TDD exception for human-facing policy prose: do not create pytest or script checks that merely search for required sentences.

- [ ] **Step 2: Separate the historical 0.x policy from the binding 1.x policy**

Keep the existing 0.x rules as historical context and add a binding `## 1.x policy` section with these exact decisions:

- Semantic Versioning governs 1.x.
- Stable REST, MCP, CLI, configuration schema, import/export, backup format, and Provider Plugin API remain backward-compatible within 1.x.
- Stable removals or incompatible changes require the next major version; optional fields/capabilities may be added in 1.x.
- Beta and experimental contracts may change only in a minor release with changelog and migration instructions.
- Experimental contracts have no compatibility window but must be visibly marked.
- SQLite migrations remain immutable and forward-only. Before an irreversible upgrade, the CLI requires a verified backup; rollback means restoring that backup with the old binary, never downgrading the live schema.
- Unknown future configuration/backup versions and Plugin API major mismatches fail explicitly.
- The 0.x-to-1.x configuration break is handled only by the Phase 2 `hl-mem config migrate` path; do not promise indefinite aliases here.

- [ ] **Step 3: Run the complete Phase 1 gate**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/unit/test_request_size_limit.py tests/unit/test_recall_historical_auxiliary.py tests/unit/test_database_lifecycle.py tests/unit/test_runtime_resource_ownership.py -q --tb=short
& '.\.venv\Scripts\python.exe' -W error::ResourceWarning -m pytest tests/ -q --tb=short
& '.\.venv\Scripts\python.exe' -m ruff check src tests scripts
& '.\.venv\Scripts\python.exe' -m black --check .
& '.\.venv\Scripts\python.exe' -m isort --check-only .
& '.\.venv\Scripts\python.exe' -m mypy src/hl_mem --ignore-missing-imports
& '.\.venv\Scripts\python.exe' scripts/check_imports.py
& '.\.venv\Scripts\python.exe' scripts/check_docs_consistency.py
& '.\.venv\Scripts\python.exe' scripts/check_openapi_snapshot.py
& '.\.venv\Scripts\python.exe' scripts/check_mcp_snapshot.py
```

Expected: every command PASS. OpenAPI and MCP snapshots remain byte-for-byte unchanged.

- [ ] **Step 4: Review scope and commit the policy**

Run:

```powershell
git diff --check
git status --short
```

Confirm there is no migration, no Settings redesign, no Provider Registry, no graph change, and no unrelated artifact deletion.

```powershell
git add docs/compatibility.md
git commit -m "docs: define the HL-Mem 1.x compatibility policy"
```

---

## Phase 1 Completion Record

- [ ] All six task commits exist and each focused test was observed failing before its implementation.
- [ ] Python 3.12, 3.13, and 3.14 CI matrix is configured; the regenerated lock accepts Python 3.12+.
- [ ] Actual request bytes, including streamed/no-header bodies, cannot bypass the configured limit.
- [ ] Historical recall never injects current-only Policy or Derivation context.
- [ ] Full test suite passes with `ResourceWarning` promoted to error.
- [ ] Interpreter-finalization-only SQLite diagnostics are explicitly deferred to `docs/research/sqlite-connection-lifecycle.md` and do not weaken deterministic owner shutdown tests.
- [ ] 1.x compatibility and rollback-by-restore policies are explicit.
- [ ] Public OpenAPI and MCP snapshots are unchanged.
- [ ] Unrelated user files remain untouched.
- [ ] Only after this record is complete: author `2026-08-30-hl-mem-core-1-0-phase-2-config.md` against the merged Phase 1 code using `superpowers:writing-plans`.
