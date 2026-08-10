# 提取架构最终版 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将逐 Event LLM 提取改成生产与 LongMemEval 共用的同 session 有界微批，同时保留 Event 级 speaker、turn、时间与证据映射。

**Architecture:** Event 与 `extract_event` job 仍一一持久化；Worker 在租约事务内临时合并同 tenant/session 的普通 message jobs。Extractor 接收结构化 conversation，claim 通过 `source_event_indices` 选择来源 Event，现有 evidence 表持久化多来源映射。Hermes `sync_turn` 使用新增的原子 batch API 写入 user/assistant 配对。

**Tech Stack:** Python 3.11、FastAPI、Pydantic v2、SQLite WAL、pytest（仅 GitHub Actions 执行）、Ruff、mypy、uv。

## Global Constraints

- 不修改评测数据集、检索阈值或 QA 判定。
- 保留 `/v1/events`、单 Event job payload、导入归档和现有数据库兼容。
- 窗口固定为 `max_events=4`、`max_wait_seconds=2.0`；不增加 idle/token/adaptive 参数。
- 不新增 SQL migration；Event turn 使用既有 `metadata_json`。
- 本地不运行 pytest；测试先写，静态检查后由 GitHub Actions 验证。
- 不触碰工作区已有的 3 个未跟踪评测文件。

---

### Task 1: 窗口配置与原子多 job lease

**Files:**
- Modify: `src/hl_mem/settings.py`
- Modify: `src/hl_mem/storage/jobs.py`
- Test: `tests/unit/test_extraction_batching.py`
- Modify: `config.example.toml`

**Interfaces:**
- Produces: `Settings.extraction_batch_max_events: int = 4`
- Produces: `Settings.extraction_batch_max_wait_seconds: float = 2.0`
- Produces: `JobRepository.lease_job(..., extraction_batch_max_events=1, extraction_batch_max_wait_seconds=0.0, force_extraction=False)`
- Produces: leased extraction job contains `leased_job_ids` and `payload={"event_ids": [...]}`.
- Produces: `complete_jobs(...)` and `fail_jobs(...)` atomically finish all jobs sharing a lease token.

- [ ] **Step 1: Write lease behavior tests**

Create real SQLite events/jobs covering: four same-session messages lease together; another tenant/session stays pending; a young partial window returns `None`; an aged partial window leases; force leases immediately; explicit memory leases alone; complete/fail updates every leased id.

```python
leased = jobs.lease_job(
    lease,
    now,
    extraction_batch_max_events=4,
    extraction_batch_max_wait_seconds=2.0,
    force_extraction=True,
)
assert leased["payload"] == {"event_ids": ["e0", "e1", "e2", "e3"]}
assert leased["leased_job_ids"] == ["j0", "j1", "j2", "j3"]
```

- [ ] **Step 2: Confirm expected RED statically**

Do not run pytest. Confirm the new tests import missing Settings fields/method behavior before production edits with `rg` and `python -m compileall tests/unit/test_extraction_batching.py`.

- [ ] **Step 3: Implement minimal settings and lease logic**

Select runnable candidates inside one `BEGIN IMMEDIATE`. For ordinary message Event jobs, query matching available jobs by `events.tenant_id/session_id`, order by Event time, cap at `max_events`, and lease only when full, aged, or forced. Preserve the old single-job behavior when `max_events=1`.

- [ ] **Step 4: Add bulk terminal updates**

Use one transaction and `id IN (...) AND lease_token=? AND status='running'`; on failure compute pending/dead per job from attempts/max_attempts.

- [ ] **Step 5: Run static verification**

Run Ruff on the changed files and `python -m compileall` on source/tests. Expected: exit 0.

### Task 2: 原子 turn ingest 与 Hermes 配对

**Files:**
- Modify: `src/hl_mem/application/ingest.py`
- Modify: `src/hl_mem/api/schemas.py`
- Modify: `src/hl_mem/api/server.py`
- Modify: `src/hl_mem/adapters/hermes/provider.py`
- Test: `tests/unit/test_extraction_batching.py`
- Modify: `tests/unit/test_provider.py`

**Interfaces:**
- Produces: `IngestService.ingest_events(events) -> list[{id, created}]`.
- Produces: `EventBatchInput.events`, length 1..4.
- Produces: `POST /v1/events/batch` response `{"events": [...]}`.
- Consumes: Event `metadata.turn_id` for persistent turn mapping.

- [ ] **Step 1: Write ingest/API/provider tests**

Assert batch insertion creates two Events/two existing-style jobs in one transaction; enqueue failure rolls both back; old `/v1/events` response is unchanged; `sync_turn` makes exactly one `/v1/events/batch` request whose Event roles are user/assistant and whose `metadata.turn_id` values match.

- [ ] **Step 2: Refactor ingest_event through ingest_events**

Move stored Event preparation and idempotency comparison into helpers, loop under one `BEGIN IMMEDIATE`, and return the first result from `ingest_event`. Include metadata in canonical idempotency payload.

- [ ] **Step 3: Add API and provider call**

Validate at most four Events per API request. In `sync_turn`, use the supplied `turn_id` or a generated UUID and send both Events in one request. Other write hooks continue using `/v1/events`.

- [ ] **Step 4: Static verification**

Run Ruff and compileall. Expected: exit 0.

### Task 3: Claim source indices and multi-Event evidence

**Files:**
- Modify: `src/hl_mem/ingest/schemas.py`
- Modify: `src/hl_mem/ingest/extractors.py`
- Modify: `src/hl_mem/ingest/llm_extractor.py`
- Modify: `src/hl_mem/application/ingest.py`
- Test: `tests/unit/test_extraction_batching.py`
- Modify: `tests/unit/test_llm_extractor.py`
- Modify: `tests/unit/test_extraction_prompt_quality.py`

**Interfaces:**
- Produces: `ExtractedClaim.source_event_indices: tuple[int, ...]`.
- Produces: compact schema field `source_event_indices: list[int]`, max 4.
- Produces: `IngestService.store_extracted(..., source_events=None)`.

- [ ] **Step 1: Write source-mapping tests**

Use a fake LLM response to prove valid indices survive postprocessing, a batch response missing indices is rejected, out-of-range indices are rejected, and duplicate chunk claims union their indices. Persist one claim with two source Events and assert two `derived_from` links.

```python
assert claim.source_event_indices == (0, 1)
links = EvidenceRepository(connection).get_links_for_derived("claim", claim_id)
assert {link["evidence_id"] for link in links if link["relation"] == "derived_from"} == {"e0", "e1"}
```

- [ ] **Step 2: Extend prompt/schema without breaking single Event callers**

Prompt exactly seven fields and require indices for multi-Event input. Compact Pydantic defaults to `[0]` for legacy single Event responses, while postprocessing inspects `model_fields_set` and rejects omitted indices when `_source_events` has more than one item. Raise the per-chunk cap from 10 to 20 claims.

- [ ] **Step 3: Validate evidence against referenced source only**

Build admission source text from the referenced Event contents, not the whole batch. Resolve occurrence time from the first referenced Event. Private `_source_events` context must not be serialized into the LLM prompt.

- [ ] **Step 4: Link all source Events atomically**

Replace each single `_link_event` branch in `store_extracted` with one helper that links the primary and additional source Events plus their image-description evidence before commit.

- [ ] **Step 5: Static verification**

Run Ruff and compileall. Expected: exit 0.

### Task 4: Worker batch processing

**Files:**
- Modify: `src/hl_mem/workers/worker.py`
- Test: `tests/unit/test_extraction_batching.py`
- Modify: `tests/unit/test_worker.py`

**Interfaces:**
- Consumes: batched job payload `event_ids` and claim source indices.
- Produces: `Worker.run_once(force_extraction=False)` token/count metrics.
- Produces: optional external SQLite connection for benchmark reuse.

- [ ] **Step 1: Write worker behavior tests**

Capture one extractor call for user/assistant Events and assert `content.messages` has stable indices, roles, turns, times and content. Return claims mapped to different Event indices and assert correct evidence. Cover per-Event filter skip and explicit-memory single-job bypass.

- [ ] **Step 2: Extract per-Event preparation helper**

Keep image description, EventFilter and PreFilter semantics per Event. Return only eligible Event/content pairs; skipped jobs still finish with the batch.

- [ ] **Step 3: Build one structured conversation call**

For LLMExtractor send `{"messages": [...]}` plus public metadata and private `_source_events`. For non-LLM extractors, call per Event and assign its source index so fake/test mode remains deterministic.

- [ ] **Step 4: Finish all leased jobs together**

`run_once` uses batch-aware lease/complete/fail, returns `events`, `claims`, `stored`, `input_tokens`, `output_tokens`, `total_tokens`, and never holds a lease transaction during the LLM request.

- [ ] **Step 5: Static verification**

Run Ruff and compileall. Expected: exit 0.

### Task 5: LongMemEval production-path alignment

**Files:**
- Modify: `evaluation/tools/run_longmemeval_benchmark.py`
- Modify: `tests/unit/test_longmemeval_benchmark_script.py`

**Interfaces:**
- Consumes: `Worker(..., connection=connection)` and `run_once(force_extraction=True)`.
- Produces: unchanged ingest metric keys plus actual production-window token totals.

- [ ] **Step 1: Write benchmark path test**

Patch Worker with a recording double and assert `_ingest_case` queues all Events, drains Worker until idle, accumulates returned counts/tokens, and never calls a direct extractor/store branch.

- [ ] **Step 2: Replace direct extraction loop**

Keep Event ids, contents, timestamps and locator data. Add turn metadata, ingest all Events, construct the real Worker with the same extractor/embedder and an unlimited benchmark budget, then force-drain the queue.

- [ ] **Step 3: Invalidate old extraction caches**

Change `EXTRACTION_FRAGMENT_PROTOCOL_VERSION` to `production-microbatch-v1`; leave dataset and thresholds untouched.

- [ ] **Step 4: Static verification**

Run Ruff and compileall. Expected: exit 0.

### Task 6: Release docs, snapshots, verification and delivery

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/hl_mem/__init__.py`
- Modify: `uv.lock`
- Modify: `README.md`
- Modify: `docs/CHANGELOG.md`
- Modify: `docs/api.md`
- Modify: `docs/api-schema.json`
- Modify: `docs/configuration.md`
- Modify: `docs/architecture.md`
- Modify: `docs/capability-matrix.md`

**Interfaces:**
- Produces: release identity v0.25.0; OpenAPI 17 routes; 38 migrations unchanged.

- [ ] **Step 1: Update version and docs**

Document the two window settings, 2～4 second visibility, `/v1/events/batch`, multi-event evidence and rollback setting `batch_max_events=1`.

- [ ] **Step 2: Regenerate OpenAPI snapshot**

Use the repository's existing snapshot mechanism or a short read-only Python invocation of `create_app(...).openapi()`; do not manually edit generated JSON.

- [ ] **Step 3: Run full allowed local verification**

Run `ruff check`, `ruff format --check`, `mypy` if configured, `python -m compileall`, `git diff --check`, and relevant snapshot/version checks. Do not run pytest.

- [ ] **Step 4: Review requirements and diff**

Confirm every design constraint has a code/test/doc owner, no evaluation thresholds changed, no migration exists, and unrelated untracked files are absent from the diff.

- [ ] **Step 5: Commit and push**

Commit implementation with `feat: add bounded extraction microbatching`, push `main` to `origin`, then inspect the triggered GitHub Actions run to completion.

- [ ] **Step 6: Report measured evidence**

Report local static command outputs, remote CI conclusion, commit hashes, 50/500 Event ideal request and fixed-token estimates, production behavior, known warnings, risk and rollback.
