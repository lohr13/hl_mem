# Batch 1A — P1-1 + P1-2 + P2-9

> 共识方案详见 docs/refactor-phase13-consensus.md

## P1-1: 幂等写入竞态修复

### 目标文件: `src/hl_mem/application/ingest.py`

### 改动: `IngestService.ingest_event()` 方法（当前第 65-103 行）

**当前问题**: 第 72-76 行的幂等键查询在 `BEGIN IMMEDIATE`（第 94 行）之前执行。

**改为**: 删除事务外查询，将幂等查询移入 `BEGIN IMMEDIATE` 事务内部。

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
        # 事务内检查幂等键
        if key:
            existing = self.connection.execute(
                "SELECT id FROM events WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing:
                self.connection.commit()
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

## P1-2: 去重 TOCTOU 竞态修复

### 目标文件: `src/hl_mem/application/ingest.py`

### 改动: `IngestService.store_extracted()` 静态方法（当前第 154-304 行）

**当前问题**: fact_hash 去重（219）、conflict_key 查询（230）、semantic dedup（261）全在事务外（第 273 行才 BEGIN IMMEDIATE）。

**改为**:
1. 事务外只做 claim 规范化 + embedding 计算
2. `BEGIN IMMEDIATE` 后依次重新执行 exact dedup → conflict → semantic dedup
3. **关键（Codex 修正）**: `insert_claim()` 返回 False 时，重新查询实际胜出的 claim，给它添加 evidence 并返回其 ID（不能丢失证据链）
4. audit 在 commit 后发出（或至少减少锁内工作量）

**具体重构**:
```python
@staticmethod
def store_extracted(connection, extracted, event, now, embedder, authority=None, ttl_days=7):
    audit = current_audit()
    claims_repo, evidence_repo = ClaimRepository(connection), EvidenceRepository(connection)
    namespace = event.get("tenant_id", "default")
    subject = normalize_entity_id(extracted.subject)
    qualifiers = extracted.qualifiers or {}
    canonical_attribute = validate_canonical_attribute(...)
    value_json = json.dumps(extracted.value, ensure_ascii=False, sort_keys=True)
    scope = extracted.scope if extracted.scope in {"temporal", "permanent"} else "permanent"
    # ... TTL 计算（同现有逻辑）...
    importance = ...（同现有逻辑）

    # 事务外：构建 claim dict + 计算 embedding
    claim = { ... }（同现有字段）
    claim["embedding_dense"] = embedder.embed_one(claim_text(claim))

    # 事务内：所有 DB 判定
    connection.execute("BEGIN IMMEDIATE")
    try:
        # 1. exact dedup（事务内）
        exact = claims_repo.find_by_fact_hash(namespace, claim["fact_hash"])
        if exact:
            _link_event(evidence_repo, exact["id"], event["id"], commit=False)
            connection.commit()
            return exact["id"]

        # 2. conflict 判定（事务内）
        exclusive = is_mutually_exclusive_attribute(canonical_attribute)
        existing = claims_repo.find_by_conflict_key(claim["conflict_key"]) if exclusive else []
        superseded_old_id = None
        resolution = None
        current = None

        if existing:
            current = existing[0]
            resolution = ConflictResolver().resolve(current, {**claim, "qualifiers": qualifiers})
            if resolution == "entails":
                _link_event(evidence_repo, current["id"], event["id"], commit=False)
                connection.commit()
                return current["id"]
            if resolution == "state_change":
                claim["supersedes_id"] = current["id"]
                superseded_old_id = current["id"]
            elif resolution == "contradicts":
                claim["status"] = "disputed"
            elif resolution == "uncertain":
                claim["status"] = "candidate"
        else:
            # 3. semantic dedup（事务内）
            duplicate_id, _ = Deduplicator(claims_repo, embedder).find_duplicate(claim)
            if duplicate_id:
                _link_event(evidence_repo, duplicate_id, event["id"], commit=False)
                connection.commit()
                return duplicate_id

        # 4. insert claim
        inserted = claims_repo.insert_claim(claim, commit=False)

        # 5. 关键修正：insert 失败时重查胜出 claim
        if not inserted:
            # 另一个 worker 已插入了相同记录
            winner = claims_repo.find_by_fact_hash(namespace, claim["fact_hash"])
            if winner:
                _link_event(evidence_repo, winner["id"], event["id"], commit=False)
            connection.commit()
            return winner["id"] if winner else claim["id"]

        # 6. conflict case 记录
        if current is not None and resolution in {"contradicts", "uncertain"}:
            if current and resolution == "contradicts":
                claims_repo.update_status(current["id"], "disputed", commit=False)
            connection.execute(
                "INSERT OR IGNORE INTO conflict_cases ...", (...)（同现有逻辑）
            )

        # 7. supersede
        if superseded_old_id:
            claims_repo.supersede_with_inline(
                superseded_old_id, claim["id"], extracted.value, claim["valid_from"], now, commit=False
            )

        _link_event(evidence_repo, claim["id"], event["id"], commit=False)
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    # audit 在 commit 后
    audit.emit(...)（各项 audit 移到这里或减少）

    return claim["id"]
```

**注意**:
- `_link_event_atomically` 函数（第 315-322 行）不再需要（因为 _link_event 已经在事务内调用）。保留函数但标记 deprecated。
- 注意 `_link_event` 第 307-312 行的 `commit` 参数：事务内调用时传 `commit=False`。

## P2-9: 新增并发测试

### 新增文件: `tests/unit/test_concurrency.py`

```python
"""并发写入和 lease 竞态测试。"""
import threading
import time
from hl_mem.storage.database import Database
from hl_mem.storage.repository import EventRepository, JobRepository
from hl_mem.application.ingest import IngestService
from hl_mem.ingest.extractors import FakeExtractor, ExtractedClaim
from hl_mem.ingest.embeddings import FakeEmbedder


def test_concurrent_idempotent_event_write(tmp_path):
    """两个线程写相同 idempotency_key，断言只创建一个 event。"""
    db_path = tmp_path / "concurrent.db"
    db1, db2 = Database(db_path), Database(db_path)
    
    barrier = threading.Barrier(2)
    results = [None, None]
    
    def write(idx, db):
        conn = db.open()
        service = IngestService(conn, FakeEmbedder(2048))
        barrier.wait()  # 同时开始
        results[idx] = service.ingest_event(
            {"event_type": "message", "actor_type": "user", "content": {"text": "test"}},
            idempotency_key="same-key"
        )
    
    t1 = threading.Thread(target=write, args=(0, db1))
    t2 = threading.Thread(target=write, args=(1, db2))
    t1.start(); t2.start()
    t1.join(); t2.join()
    
    # 断言：两个线程返回相同的 event_id
    assert results[0]["id"] == results[1]["id"]
    # 断言：只有一个 created=True
    assert results[0]["created"] is True or results[1]["created"] is True
    assert not (results[0]["created"] and results[1]["created"])
    # 断言：DB 中只有一个 event
    conn = db1.open()
    count = conn.execute("SELECT count(*) FROM events WHERE idempotency_key='same-key'").fetchone()[0]
    assert count == 1


def test_concurrent_claim_dedup(tmp_path):
    """两个线程写相同 fact_hash 的 claim，断言只创建一个 active claim。"""
    from hl_mem.api.pipeline import store_extracted
    from hl_mem.storage.database import Database
    from hl_mem.ingest.embeddings import FakeEmbedder
    from hl_mem.ingest.extractors import ExtractedClaim
    
    db_path = tmp_path / "dedup.db"
    db1, db2 = Database(db_path), Database(db_path)
    db1.open_worker(); db2.open_worker()
    
    barrier = threading.Barrier(2)
    results = [None, None]
    
    def store(idx, db):
        conn = db.open_worker()
        extracted = ExtractedClaim(
            predicate="likes", value="coffee", confidence=0.9,
            volatility="stable", subject="user", qualifiers={},
            scope="permanent", importance=0.8,
            canonical_attribute=None,
        )
        event = {"id": f"event-{idx}", "actor_type": "user", "occurred_at": "2026-01-01T00:00:00+00:00"}
        barrier.wait()
        results[idx] = store_extracted(conn, extracted, event, "2026-01-01T00:00:00+00:00", FakeEmbedder(2048))
    
    t1 = threading.Thread(target=store, args=(0, db1))
    t2 = threading.Thread(target=store, args=(1, db2))
    t1.start(); t2.start()
    t1.join(); t2.join()
    
    # 两个结果应该指向同一个 claim
    assert results[0] == results[1]
    # DB 中只有一个 active claim
    conn = db1.open_worker()
    count = conn.execute("SELECT count(*) FROM claims WHERE subject_entity_id='user' AND predicate='likes' AND status='active'").fetchone()[0]
    assert count == 1
```

## 约束
- 不要修改 tests/ 目录下的任何现有测试文件（只新建 test_concurrency.py）
- 不要运行 pytest（Windows 管道兼容性问题）
- 完成后只运行 `git add src/ tests/`（不要 `git add -A`）和 `git commit`
- 版本号 bump: `src/hl_mem/__init__.py` 和 `pyproject.toml` 从 0.3.0 → 0.3.1
