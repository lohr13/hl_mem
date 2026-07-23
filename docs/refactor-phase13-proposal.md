# Phase 13 — P1+P2 全面修复方案

> Hermes 独立分析 · 2026-07-23
> 基于 Codex 审查报告（13 个问题：7 P1 + 6 P2）

## P1 修复方案

### P1-1: 幂等写入竞态

**位置**: `application/ingest.py:71-103`

**当前问题**: `ingest_event()` 在第 72-76 行查询 idempotency_key，此时还没进事务。两个并发请求都可能查到"不存在"，然后 A 先 `INSERT` 成功，B 的 `INSERT OR IGNORE` 被忽略但 B 返回自己生成的 event_id——这个 ID 在 DB 里不存在。

**方案**: 把幂等查询移进 `BEGIN IMMEDIATE` 事务内部：
```python
def ingest_event(self, event, idempotency_key=None):
    key = idempotency_key or event.get("idempotency_key")
    event_id = event.get("id") or new_id()
    timestamp = _now()
    content = event.get("content", {})
    content = content if isinstance(content, dict) else {"text": content}
    content_json = json.dumps(content, ensure_ascii=False, sort_keys=True)
    stored_event = {k: v for k, v in event.items() if k not in {"content", "id"}}
    stored_event.update(
        id=event_id, idempotency_key=key, content_json=content_json,
        occurred_at=event.get("occurred_at") or timestamp,
        recorded_at=timestamp,
        content_hash=hashlib.sha256(content_json.encode()).hexdigest(),
    )
    self.connection.execute("BEGIN IMMEDIATE")
    try:
        # 事务内重新检查幂等键
        if key:
            existing = self.connection.execute(
                "SELECT id FROM events WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing:
                self.connection.commit()  # 释放锁
                return {"id": existing["id"], "created": False}
        created = EventRepository(self.connection).insert_event(stored_event, commit=False)
        if created:
            self._queue_event(event_id, timestamp, commit=False)
        self.connection.commit()
    except Exception:
        self.connection.rollback()
        raise
    return {"id": event_id, "created": created}
```

**新增测试**: 用两个 `Database` 实例 + `threading.Barrier` 并发写入相同 idempotency_key，断言只创建一个 event，两个线程返回相同的 event_id。

---

### P1-2: 去重/冲突判定 TOCTOU 竞态

**位置**: `application/ingest.py:218-273`

**当前问题**: `store_extracted()` 中 fact_hash 去重（219）、conflict_key 查询（230）、semantic dedup（261）全在事务外。到第 273 行才 `BEGIN IMMEDIATE`。

**方案**: 把 fact_hash 去重和 conflict_key 判定移进事务内重做（double-check 模式）。embedding 可以留在事务外（避免长时间持锁）：
```
1. [事务外] 计算 embedding
2. BEGIN IMMEDIATE
3. [事务内] 重新检查 fact_hash → 命中则合并证据返回
4. [事务内] 重新检查 conflict_key → 冲突判定
5. [事务内] 重新检查 semantic dedup（需要 embedding_dense 已算好）
6. [事务内] insert_claim + evidence_link + commit
```

**关键**: `insert_claim()` 返回值检查——`INSERT OR IGNORE` 被跳过时返回 0，此时不应继续建 evidence link。在 `_insert()` 已有返回值（bool），目前 `store_extracted` 没检查。

**注意**: semantic dedup 需要遍历已入库 claims 做余弦相似度，移入事务内会增加锁持有时间。但当前数据量小（<1000 claims），暴力扫描 <10ms，可接受。如果未来量大，可改为事务内只做 fact_hash + conflict_key，semantic 降级为 best-effort。

---

### P1-3: Job lease 无所有权令牌

**位置**: `storage/repository.py:372-420`

**当前问题**: `lease_job()` 只记 `leased_until`，`complete_job()` 和 `fail_job()` 只按 `job_id` 更新。过期 worker 恢复后可以 complete/fail 别人正在跑的 job。

**方案**:
1. `lease_job()` 生成 `lease_token = uuid4().hex`，写入 job 行，返回 token
2. `complete_job()` 和 `fail_job()` 增加 `lease_token` 参数，SQL 条件加 `AND lease_token=?`
3. 需要新增 migration `015_lease_token.sql`：`ALTER TABLE jobs ADD COLUMN lease_token TEXT`

```python
def lease_job(self, leased_until, updated_at):
    lease_token = uuid.uuid4().hex
    self.connection.execute("BEGIN IMMEDIATE")
    try:
        row = self.connection.execute(
            "SELECT id FROM jobs WHERE ...", (...)
        ).fetchone()
        if not row:
            self.connection.commit()
            return None
        self.connection.execute(
            "UPDATE jobs SET status='running',leased_until=?,updated_at=?,"
            "attempts=attempts+1,lease_token=? WHERE id=?",
            (leased_until, updated_at, lease_token, row["id"]),
        )
        self.connection.commit()
        job = _row(self.connection.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())
        job["lease_token"] = lease_token  # 返回 token 给调用者
        return job
    except Exception:
        self.connection.rollback()
        raise

def complete_job(self, job_id, updated_at, lease_token=None):
    return self._finish(job_id, "succeeded", updated_at, None, lease_token)

def _finish(self, job_id, status, updated_at, error, lease_token=None):
    if lease_token:
        cursor = self.connection.execute(
            "UPDATE jobs SET status=?,updated_at=?,last_error=?,leased_until=NULL "
            "WHERE id=? AND lease_token=?",
            (status, updated_at, error, job_id, lease_token),
        )
    else:
        # 向后兼容：无 token 时退化为旧逻辑
        cursor = self.connection.execute(
            "UPDATE jobs SET status=?,updated_at=?,last_error=?,leased_until=NULL WHERE id=?",
            (status, updated_at, error, job_id),
        )
    self.connection.commit()
    return cursor.rowcount == 1
```

Worker 调用侧 `run_once()` 保存 lease_token，complete/fail 时传入。`fail_job` 内部先读 attempts 的逻辑也需要加 token 条件保护。

---

### P1-4: 上下文预算可超限

**位置**: `application/recall.py:108-115`

**当前问题**: `if packed and used + cost > token_budget` —— 第一项超过预算时 `packed` 为空列表（falsy），跳过检查。

**方案**: 改为无条件检查，但允许第一条截断后返回：
```python
for item in all_items:
    data = item["data"]
    text = str(data.get("text") or data.get("body") or data.get("procedure") or "")
    cost = max(1, (len(text) + 1) // 2)
    if used + cost > token_budget:
        truncated = True
        if not packed:
            # 第一条超预算：截断文本而不是跳过
            max_chars = max(1, token_budget * 2 - 2)
            data = {**data, "_truncated": True}
            data["text"] = text[:max_chars]
            cost = max(1, (len(data["text"]) + 1) // 2)
            packed.append({**item, "data": data})
            used += cost
        continue
    packed.append(item)
    used += cost
    if used >= token_budget:
        truncated = len(packed) < len(all_items)
        break
```

新增测试：第一条 claim 的 text 长度超过 token_budget，断言 `used_tokens_estimate <= token_budget`。

---

### P1-5: API 无输入体积上限

**位置**: `api/schemas.py:26-58`

**方案**: 在 Pydantic 模型字段上加 `max_length`：
- `EventInput.content`: 添加 validator 检查序列化后字节数 ≤ 500KB
- `RecallInput.query`: `max_length=2000`
- `MemoryInput.text/content`: `max_length=50000`
- `EpisodeInput.goal`: `max_length=5000`
- `TraceInput.action`: `max_length=10000`
- `TraceInput.observation`: `max_length=50000`
- `FeedbackInput.task_outcome`: `max_length=5000`
- `MemoryInput.qualifiers`: 限制嵌套深度 + 序列化大小

用 `@field_validator` 做序列化大小检查，超限返回 422。

同时给 uvicorn 加请求体上限（ASGI 层），或给 FastAPI 加 middleware。

---

### P1-6: Recall 无 namespace 过滤

**位置**: `api/schemas.py:32` + `storage/repository.py:157,328` + `application/recall.py:51`

**方案**:
1. `RecallInput` 加 `namespace: str = "default"` 字段
2. `RecallService.recall()` 接收 `namespace` 参数
3. 传给 `hybrid_claims()` → `ClaimRepository` 的 FTS/vector/scan 查询
4. `list_embedded()`、`search_claims_vector()`、FTS 查询都加 `WHERE namespace_key=?`
5. `_assemble_results()` 中的 rivals 查询也加 namespace 条件
6. API 层 `/v1/recall` 从 payload 中取 namespace，默认 `"default"`

**侵入面**: 需要改 `ClaimRepository` 的 4-5 个方法签名 + `recall_pipeline.py` 的 `hybrid_claims()` + `RecallService` + API DTO。中等规模改动。

**降级**: 如果暂不做完整 namespace 隔离，至少在 RecallService 和 repository 层固定传 `"default"`，保证存储层不执行全库查询。

---

### P1-7: Recall N+1 查询

**位置**: `application/recall.py:155-197`

**当前问题**: `_assemble_results()` 对每条 claim 循环调用：
- `evidence_repo.get_links_for_derived("claim", claim["id"])` — 1 次/claim
- `claim_repo.get_claim(superseded_by_id)` — 1 次/claim（有 supersede 时）
- `get_relations(connection, claim["id"])` — 1 次/claim
- rivals SQL — 1 次/claim（disputed 时）

**方案**: 批量加载：
1. 收集所有 claim_id
2. 一次性查 `WHERE derived_type='claim' AND derived_id IN (...)` 加载所有 evidence links
3. 一次性查 `WHERE id IN (...)` 加载所有 superseded claims
4. relations 批量查
5. disputed rivals 批量查 `WHERE conflict_key IN (...) AND status='disputed'`
6. Python 中按 ID 分组组装

新增 `ClaimRepository.batch_get_claims(ids)` 和 `EvidenceRepository.batch_get_links(derived_ids)` 方法。

---

## P2 修复方案

### P2-8: healthz 测试漂移

**位置**: `tests/integration/test_e2e.py:48-55`

**方案**: 把硬编码的完全相等断言改为只断言稳定字段：
```python
def test_healthz(tmp_path):
    with TestClient(create_app(tmp_path / "health.db")) as client:
        result = client.get("/healthz").json()
        assert result["status"] == "ok"
        assert result["version"] == __version__  # 从 hl_mem import __version__
        assert "embedder" in result
        assert "reranker" in result
```

### P2-9: 并发测试缺失

**方案**: 新增 `tests/unit/test_concurrency.py`：
- `test_concurrent_idempotent_event_write`: 两线程写相同 idempotency_key，断言只创建一个 event
- `test_concurrent_claim_dedup`: 两线程写相同 fact_hash 的 claim，断言只一个 active
- `test_lease_token_prevents_old_worker`: 模拟 lease 过期 → 新 worker 领取 → 旧 worker complete 被拒

### P2-10: 无覆盖率工具

**方案**:
1. `pyproject.toml` dev deps 加 `pytest-cov`
2. `pyproject.toml` 加 `[tool.coverage]` 配置
3. 不加硬门槛（先记录基线）
4. `e2e_real.py` 重命名为 `test_e2e_real.py`，加 `@pytest.mark.real_api` 标记，conftest.py 注册 marker + 默认跳过

### P2-11: HTTP 连接不复用

**方案**: 给 Embedder、LLMExtractor、Reranker 的构造函数加可选 `client: httpx.Client | None = None`。components.py 创建共享 `httpx.Client` 并注入。app/worker 生命周期负责 close。
- 统一 `retry_http()` 使用（已有 `http_utils.py`）
- 不改变现有降级语义

### P2-12: real 模式静默降级

**方案**: `components.py` 区分两种情况：
- 环境变量未设置（默认）→ fake（开发友好）
- 环境变量显式设为 `real` 但缺 key → 抛 `ConfigurationError`
- 新增 `HL_MEM_ALLOW_FAKE_FALLBACK=true` 允许显式降级

### P2-13: domain 层不纯

**方案**: `domain/entity.py` 的 `_load_aliases()` 改为接收 alias dict 参数（而非自己读环境变量/文件）。由 `components.py` 或 `settings.py` 在启动时加载并注入。

---

## 实施批次建议

| 批次 | 内容 | 风险 | 预计改动 |
|------|------|------|----------|
| **Batch 1** | P1-1 + P1-2 + P1-3 + P2-8 + P2-9 | 数据正确性，互相耦合 | ingest.py, repository.py, worker.py, migration, tests |
| **Batch 2** | P1-4 + P1-5 + P1-7 | 召回+API | recall.py, schemas.py, repository.py |
| **Batch 3** | P1-6 | namespace 隔离，侵入面大 | schemas.py, recall.py, repository.py, recall_pipeline.py |
| **Batch 4** | P2-10 + P2-11 + P2-12 + P2-13 | 工程质量 | components.py, entity.py, pyproject.toml, conftest.py |

每个 batch 独立 commit + push + 验收。
