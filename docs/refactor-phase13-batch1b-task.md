# Batch 1B — P1-3 Job Lease Token

> 共识方案详见 docs/refactor-phase13-consensus.md

## 目标

给 Job lease 加所有权令牌（lease_token），防止过期 worker 覆盖新 worker 的结果。

**关键约束（Codex 共识修正）**: token **必填**，不允许 `lease_token=None` 退化为旧逻辑。所有调用方一次性迁移。

## 改动

### 1. 新增 migration: `src/hl_mem/storage/migrations/015_lease_token.sql`

```sql
ALTER TABLE jobs ADD COLUMN lease_token TEXT;
```

### 2. `src/hl_mem/storage/repository.py` — JobRepository

#### `lease_job()` 改动:
- 生成 `lease_token = uuid4().hex`
- UPDATE 语句中设置 `lease_token=?`
- 返回的 dict 中包含 `lease_token` 字段

```python
def lease_job(self, leased_until: str, updated_at: str) -> dict[str, Any] | None:
    import uuid
    lease_token = uuid.uuid4().hex
    self.connection.execute("BEGIN IMMEDIATE")
    try:
        row = self.connection.execute(
            "SELECT id FROM jobs WHERE (status='pending' OR "
            "(status='running' AND leased_until<?)) "
            "AND (run_after IS NULL OR run_after<=?) ORDER BY created_at,id LIMIT 1",
            (updated_at, updated_at),
        ).fetchone()
        if not row:
            self.connection.commit()
            return None
        cursor = self.connection.execute(
            "UPDATE jobs SET status='running',leased_until=?,updated_at=?,attempts=attempts+1,lease_token=? "
            "WHERE id=? AND (status='pending' OR (status='running' AND leased_until<?))",
            (leased_until, updated_at, lease_token, row["id"], updated_at),
        )
        self.connection.commit()
        if cursor.rowcount != 1:
            return None
        result = _row(self.connection.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())
        if result:
            result["lease_token"] = lease_token
        return result
    except Exception:
        self.connection.rollback()
        raise
```

#### `complete_job()` 改动:
- 增加 `lease_token: str` **必填**参数
- SQL 加 `AND lease_token=? AND status='running'`

```python
def complete_job(self, job_id: str, updated_at: str, lease_token: str) -> bool:
    return self._finish(job_id, "succeeded", updated_at, None, lease_token)
```

#### `fail_job()` 改动:
- 增加 `lease_token: str` **必填**参数
- attempts 读取与状态更新在同一事务内，按 token 限定

```python
def fail_job(self, job_id: str, error: str, updated_at: str, lease_token: str) -> bool:
    self.connection.execute("BEGIN IMMEDIATE")
    try:
        row = self.connection.execute(
            "SELECT attempts,max_attempts FROM jobs WHERE id=? AND lease_token=?",
            (job_id, lease_token),
        ).fetchone()
        if not row:
            self.connection.commit()
            return False
        status = "dead" if row["attempts"] >= row["max_attempts"] else "pending"
        result = self._finish(job_id, status, updated_at, error, lease_token)
        return result
    except Exception:
        self.connection.rollback()
        raise
```

#### `_finish()` 改动:
- 增加 `lease_token: str` 参数
- SQL 加 `AND lease_token=? AND status='running'`
- 清空 `lease_token=NULL`

```python
def _finish(self, job_id: str, status: str, updated_at: str, error: str | None, lease_token: str) -> bool:
    cursor = self.connection.execute(
        "UPDATE jobs SET status=?,updated_at=?,last_error=?,leased_until=NULL,lease_token=NULL "
        "WHERE id=? AND lease_token=?",
        (status, updated_at, error, job_id, lease_token),
    )
    self.connection.commit()
    return cursor.rowcount == 1
```

#### 新增 `force_finish_job()`:
```python
def force_finish_job(self, job_id: str, status: str, updated_at: str, error: str | None = None) -> bool:
    """管理员强制结束 job，不需要 lease_token。"""
    cursor = self.connection.execute(
        "UPDATE jobs SET status=?,updated_at=?,last_error=?,leased_until=NULL,lease_token=NULL WHERE id=?",
        (status, updated_at, error, job_id),
    )
    self.connection.commit()
    return cursor.rowcount == 1
```

### 3. `src/hl_mem/workers/worker.py` — Worker

#### `run_once()` 改动:
- 保存 `lease_token` 从 lease_job 返回值
- complete_job/fail_job 传入 token

```python
def run_once(self) -> dict[str, Any]:
    now = _now()
    lease = (datetime.now(timezone.utc) + timedelta(minutes=WORKER_JOB_LEASE_MINUTES)).isoformat()
    job = self.jobs.lease_job(lease, now)
    if not job:
        return {"status": "idle"}
    lease_token = job.get("lease_token", "")
    try:
        result = self._dispatch(job)
        self.jobs.complete_job(job["id"], _now(), lease_token)
        return {"status": "succeeded", "job_id": job["id"], **result}
    except Exception as error:
        self.jobs.fail_job(job["id"], str(error), _now(), lease_token)
        current = self.connection.execute("SELECT status,attempts FROM jobs WHERE id=?", (job["id"],)).fetchone()
        return {
            "status": current["status"] if current else "unknown",
            "job_id": job["id"],
            "attempts": current["attempts"] if current else 0,
            "error": str(error),
        }
```

### 4. 现有测试更新

#### `tests/unit/test_worker.py` 第 50-56 行
`lease_job` 调用现在返回包含 `lease_token` 的 dict。`complete_job` / `fail_job` 需要 token 参数。

找到所有调用 `complete_job` / `fail_job` 的测试，添加 lease_token 参数。`lease_job` 返回的 dict 里有 token。

**不要修改 tests/ 下的现有测试。** 只新建 lease token 测试到 test_concurrency.py。

### 5. 新增测试: `tests/unit/test_concurrency.py` 追加

```python
def test_lease_token_prevents_old_worker_completion(tmp_path):
    """lease 过期后旧 worker 的 complete 被拒绝。"""
    import uuid
    from datetime import datetime, timezone
    from hl_mem.storage.repository import JobRepository

    db_path = tmp_path / "lease.db"
    db = Database(db_path)
    db.open_worker()
    conn = db.open_worker()

    jobs = JobRepository(conn)
    now = datetime.now(timezone.utc).isoformat()

    # 插入一个 job
    jobs.insert_job({
        "id": "job-1", "job_type": "extract_event",
        "payload_json": "{}", "idempotency_key": "test-1",
        "created_at": now, "updated_at": now,
    })

    # worker A 领取
    job_a = jobs.lease_job("2999-01-01T00:00:00+00:00", now)
    assert job_a is not None
    token_a = job_a["lease_token"]

    # worker B 用不同连接，lease 已过期但拿到新 token
    db2 = Database(db_path)
    conn2 = db2.open_worker()
    jobs2 = JobRepository(conn2)
    past_now = "2026-01-01T00:00:00+00:00"
    # lease 未过期（设为 2999 年），所以 B 不应该能领取
    job_b = jobs2.lease_job("2999-01-01T00:00:00+00:00", now)
    assert job_b is None  # lease 未过期，B 不能领取

    # 模拟 lease 过期：设 leased_until 为过去
    conn2.execute("UPDATE jobs SET leased_until='2000-01-01T00:00:00+00:00' WHERE id='job-1'")
    conn2.commit()

    # 现在 B 可以领取
    job_b = jobs2.lease_job("2999-01-01T00:00:00+00:00", now)
    assert job_b is not None
    token_b = job_b["lease_token"]
    assert token_b != token_a

    # worker A 尝试用旧 token 完成 → 应该失败
    success = jobs.complete_job("job-1", now, token_a)
    assert success is False

    # worker B 用新 token 完成 → 成功
    success = jobs2.complete_job("job-1", now, token_b)
    assert success is True

    db.close()
    db2.close()
```

## 约束
- 不要修改 tests/ 目录下的任何现有测试文件（只追加到 test_concurrency.py）
- 不要运行 pytest
- 完成后运行 `git add src/ tests/` 和 `git commit`
- 版本号 0.3.1 → 0.3.2
