# Phase 1：数据正确性 P0 修复

## 背景

Codex 架构审查发现 5 个 P0 级别问题，本任务修复其中 4 个数据正确性问题（P0-5 连接泄漏随 P0-4 一起修）。

## 项目位置

`D:/workspace/hl_agent/hl_mem/`

## 测试运行方式

```bash
.venv/Scripts/python.exe -m pytest tests/unit/ -q --tb=short
```

---

## P0-1：原子化 store_extracted()

### 问题

`api/pipeline.py:142` — contradiction 分支调用 `claims.update_status(id, "disputed")`，该方法内部立即 `commit()`。如果后续 embedding 生成、claim 插入或 evidence link 失败，旧 claim 已经永久变为 disputed，但没有对应的新 claim 作为对手证据。

同样，`update_status()` (repository.py:95-98) 总是 commit，无法在外部事务中使用。

### 修复方案

1. **`storage/repository.py` 的 `update_status()` 增加 `commit: bool = True` 参数**：
   ```python
   def update_status(self, claim_id: str, status: str, commit: bool = True) -> bool:
       cursor = self.connection.execute("UPDATE claims SET status=? WHERE id=?", (status, claim_id))
       if commit:
           self.connection.commit()
       return cursor.rowcount == 1
   ```

2. **`api/pipeline.py` 的 `store_extracted()` 重构事务边界**：
   - 整个写入流程（从冲突判定到 evidence link）包在同一个 `BEGIN IMMEDIATE` 事务中
   - 所有子操作（update_status、insert_claim、supersede_with_inline、_link_event）都不单独 commit
   - 最后统一 commit，异常时统一 rollback
   - **注意**：embedding 生成（`embedder.embed_one()`）是外部 HTTP 调用，应在事务外提前生成，避免数据库锁等待
   - 具体做法：先计算好所有需要的数据（包括 embedding），然后 `BEGIN IMMEDIATE` → 写入 → commit

   修改后的事务流程：
   ```python
   # 1. 准备阶段（无事务）：计算 fact_hash、conflict_key、embedding 等
   # 2. 冲突检测阶段（无事务）：SELECT 现有 claims，做 ConflictResolver 判定
   # 3. 写入阶段（单一事务）：BEGIN IMMEDIATE → update_status + insert_claim + supersede + evidence_link → commit
   ```

3. **`supersede_with_inline()` 和 `supersede()` 也需要增加 `commit` 参数**（和 `insert_claim` 一致）

---

## P0-2：确定性冲突候选选择

### 问题

`storage/repository.py:107-112` 的 `find_by_conflict_key()` 没有 `ORDER BY`。`api/pipeline.py:119` 直接取 `existing[-1]` 作为"当前事实"。SQLite 不保证无排序时返回行的顺序，可能取到错误的那条。

### 修复方案

1. **给 `find_by_conflict_key()` 添加确定性排序**：
   ```sql
   SELECT * FROM claims
   WHERE conflict_key=? AND status IN ('active','candidate','disputed')
   ORDER BY
     CASE status
       WHEN 'active' THEN 0
       WHEN 'disputed' THEN 1
       WHEN 'candidate' THEN 2
     END,
     valid_from DESC,
     recorded_from DESC,
     id DESC
   ```
   这样 `existing[0]` 就是最权威的"当前事实"。

2. **`api/pipeline.py` 中 `existing[-1]` 改为 `existing[0]`**（因为现在排序后 [0] 是最高优先级的）

---

## P0-3：升级 fact_hash 算法

### 问题

`api/pipeline.py:27-31` 的 `compute_fact_hash()` 直接字符串拼接 subject+predicate+value，没有分隔符。`("ab","c",v)` 和 `("a","bc",v)` 会产生相同 hash。

### 修复方案

1. **修改 `compute_fact_hash()` 使用 JSON 数组格式**：
   ```python
   def compute_fact_hash(subject: str, predicate: str, value: Any) -> str:
       stable_value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
       raw = json.dumps(
           ["fact-v2",
            unicodedata.normalize("NFKC", subject).strip().casefold(),
            unicodedata.normalize("NFKC", predicate).strip().casefold(),
            stable_value],
           ensure_ascii=False, sort_keys=True, separators=(",", ":"),
       )
       return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
   ```

2. **添加 migration `011_fact_hash_v2.sql`**：
   ```sql
   -- Recompute all fact_hash values using the v2 algorithm.
   -- This is a data migration: Python code will handle the recalculation
   -- because the old algorithm requires reading subject_entity_id + predicate + value_json.
   -- The migration marker is recorded here; actual backfill is done in Python.
   ```

3. **在 `database.py` 的 migration 钩子中添加 Python backfill**：
   - 读取所有 claims，用新算法重新计算 fact_hash
   - UPDATE 批量更新
   - 这必须在 migration 011 记录后、服务启动前执行

---

## P0-4 + P0-5：修复 MCP save→recall 链路 + 连接泄漏

### 问题

1. `mcp/server.py:33-48` — `memory_save` 只写 event 不创建 `extract_event` job，内容永远不会被提取
2. `mcp/server.py:66-70` — `memory_recall` 用 `value_json LIKE` 查询，不是正式的召回管线
3. `mcp/server.py:32` — 每次调用 `database.open()` 但从不 close
4. `mcp/server.py:62` — `memory_forget` 直接改 status 为 `retracted`，绕过生命周期守卫
5. `mcp/server.py:61` — `memory_explain` 用原始 SQL，不通过 repository

### 修复方案

**重写 `mcp/server.py`**，让所有操作复用与 REST API 相同的 repository 和 pipeline 逻辑：

```python
class McpMemoryServer:
    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in self._TOOLS:
            raise ValueError(f"unknown MCP tool: {name}")
        # 使用 context manager 管理连接
        with self.database.connect() as connection:
            if name == "memory_save":
                return self._save(connection, arguments)
            if name == "memory_recall":
                return self._recall(connection, arguments)
            if name == "memory_forget":
                return self._forget(connection, arguments)
            if name == "memory_explain":
                return self._explain(connection, arguments)
        # unreachable
```

#### memory_save 修复：
- 写入 event（保持现有的 explicit_memory 路径）
- **同时创建 extract_event job**（和 REST `/v1/events` 端点的逻辑一致）：
  ```python
  JobRepository(connection).insert_job({
      "id": uuid.uuid4().hex,
      "job_type": "extract_event",
      "payload_json": json.dumps({"event_id": event_id}),
      "idempotency_key": f"extract:{event_id}",
      "created_at": now,
      "updated_at": now,
  })
  ```
- 这样 worker 就会自动提取这个事件中的记忆

#### memory_recall 修复：
- 使用 `recall_pipeline.hybrid_claims()` 或至少用 FTS 搜索替代 LIKE：
  ```python
  from hl_mem.recall.recall_pipeline import hybrid_claims
  results = hybrid_claims(connection, query, limit=...)
  ```
- 如果 hybrid_claims 的签名太复杂（需要 embedder/reranker），至少用 FTS：
  ```python
  rows = connection.execute(
      "SELECT id,subject_entity_id,predicate,value_json,confidence,status "
      "FROM claims WHERE claims MATCH ? AND status='active' "
      "ORDER BY rank LIMIT ?",
      (query, limit),
  ).fetchall()
  ```

#### memory_forget 修复：
- 使用 `lifecycle.assert_transition()` 检查状态转换
- 清除 embedding（和 REST forget 一致）：
  ```python
  from hl_mem.lifecycle import assert_transition
  claim = ClaimRepository(connection).get_claim(memory_id)
  if claim:
      assert_transition(claim["status"], "retracted")
      connection.execute(
          "UPDATE claims SET status='retracted',embedding_dense=NULL,embedding_sparse=NULL WHERE id=?",
          (memory_id,),
      )
      connection.commit()
  ```
- 注意：需要在 `lifecycle.py` 的 ALLOWED_TRANSITIONS 中添加 `active → retracted` 和 `disputed → retracted`

#### memory_explain 修复：
- 使用 `ClaimRepository.get_claim()` 和 `EvidenceRepository.get_links_for_derived()` 替代原始 SQL

#### 连接管理修复：
- 如果 `Database` 没有 `connect()` context manager，添加一个：
  ```python
  @contextmanager
  def connect(self):
      conn = self.open()
      try:
          yield conn
      finally:
          conn.close()
  ```
- 或者在 McpMemoryServer 中用 try/finally 确保 close

---

## lifecycle.py 需要添加的状态

当前 `ALLOWED_TRANSITIONS` 缺少：
- `active → retracted`（MCP forget / REST forget）
- `disputed → retracted`

在 `src/hl_mem/lifecycle.py` 的 `ClaimStatus` 枚举和 `ALLOWED_TRANSITIONS` 中补充。

---

## 约束

1. **不要运行 pytest**（Windows 管道兼容性问题），测试由外部执行
2. **不要修改 tests/ 目录下的任何文件**
3. **向后兼容**：现有 180 个测试必须全部通过
4. **不要新增依赖**
5. **遵循项目现有代码风格**（类型标注、`from __future__ import annotations` 等）
6. **不要问任何问题**，直接实现全部修复
7. **注意**：`_build_observation()` 是已确认的死代码，不要修复或激活它
8. 完成后运行 `git add -A && git commit -m "fix(architecture): P0 data correctness — atomic transactions, deterministic conflict, fact_hash v2, MCP pipeline"`
