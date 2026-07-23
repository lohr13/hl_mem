# Batch 2A — P1-6 namespace 过滤 + P1-7 N+1 批量

> 共识方案详见 docs/refactor-phase13-consensus.md

## P1-6: Recall namespace 过滤

### 改动范围

#### 1. `src/hl_mem/api/schemas.py` — RecallInput 加 namespace 字段

```python
class RecallInput(BaseModel):
    query: str
    limit: int = Field(default=RECALL_DEFAULT_LIMIT, ge=1, le=100)
    as_of: str | None = None
    session_id: str | None = None
    intent: RecallIntent | None = None
    known_as_of: str | None = None
    token_budget: int | None = Field(default=None, ge=1)
    context_mode: str | None = Field(default=None, pattern="^(packed)$")
    namespace: str = "default"  # 新增
```

#### 2. `src/hl_mem/application/recall.py` — RecallService.recall() 接收 namespace

```python
def recall(self, query, limit=20, as_of=None, intent=None, known_as_of=None,
           query_id=None, token_budget=None, context_mode=None,
           namespace="default"):  # 新增
    ...
    claims = hybrid_claims(
        ClaimRepository(self.connection),
        query,
        self.embedder.embed_one(query),
        limit,
        as_of,
        self.reranker,
        intent=selected_intent,
        known_as_of=known_as_of,
        namespace=namespace,  # 新增
    )
    ...
    # policies 也要按 namespace 过滤
    policies = matching_policies(
        ExperienceService(self.connection).list_policies("active", namespace=namespace),
        query,
    )
```

#### 3. `src/hl_mem/recall/recall_pipeline.py` — hybrid_claims() 接收 namespace

```python
def hybrid_claims(repo, query, query_blob, limit, as_of, reranker=None,
                  now=None, intent=None, known_as_of=None,
                  namespace="default"):  # 新增
    ...
    fts = repo.search_claims_fts(query, candidate_limit, reference, selected_intent, known_as_of, namespace=namespace)
    ...
    dense = repo.search_claims_vector(query_blob, candidate_limit, reference, selected_intent, known_as_of, namespace=namespace)
```

#### 4. `src/hl_mem/storage/repository.py` — 查询方法加 namespace 过滤

以下方法增加 `namespace: str = "default"` 参数，SQL 加 `AND namespace_key=?`：

- `list_embedded()` — 加 `namespace_key=?` 条件
- `search_claims_vector()` — 传递给 list_embedded
- `search_claims_fts()` — 加 `AND c.namespace_key=?`
- `search_visible()` — 传递 namespace

**注意**: `namespace` 参数应为必填（去掉默认值），由上层传入。但为保持向后兼容，暂留默认值 `"default"`。

#### 5. `src/hl_mem/experience/service.py` — list_policies 加 namespace

```python
def list_policies(self, status="active", namespace="default"):
    rows = self.connection.execute(
        "SELECT * FROM policies WHERE status=? AND namespace_key=? ORDER BY updated_at DESC, id DESC",
        (status, namespace),
    ).fetchall()
    return [dict(row) for row in rows]
```

#### 6. `src/hl_mem/api/server.py` — recall 端点传 namespace

```python
@app.post("/v1/recall")
def recall(payload: RecallInput, connection=Depends(get_connection)):
    ...
    service = RecallService(connection, embedder, reranker)
    return service.recall(
        payload.query, payload.limit, payload.as_of,
        payload.intent, payload.known_as_of, None,
        payload.token_budget, payload.context_mode,
        namespace=payload.namespace,  # 新增
    )
```

#### 7. `src/hl_mem/mcp/server.py` — _recall 传 namespace

```python
def _recall(self, connection, arguments):
    ...
    return RecallService(...).recall(
        query, limit, arguments.get("as_of"), ...,
        namespace=arguments.get("namespace", "default"),
    )
```

## P1-7: Recall N+1 批量加载

### 改动: `src/hl_mem/application/recall.py` — `_assemble_results()`

当前每条 claim 触发 4 次独立查询。改为批量加载：

```python
def _assemble_results(self, claims):
    if not claims:
        return []
    claim_ids = [claim["id"] for claim in claims]
    evidence_repo = EvidenceRepository(self.connection)
    claim_repo = ClaimRepository(self.connection)

    # 批量加载 evidence links
    all_evidence = self._batch_evidence(evidence_repo, claim_ids)

    # 批量加载 superseded claims
    superseded_ids = [claim["superseded_by_id"] for claim in claims if claim.get("superseded_by_id")]
    replacement_map = self._batch_replacements(claim_repo, superseded_ids)

    # 批量加载 relations
    relations_map = self._batch_relations(claim_ids)

    # 批量加载 disputed rivals
    disputed_keys = [claim["conflict_key"] for claim in claims if claim["status"] == "disputed" and claim.get("conflict_key")]
    rivals_map = self._batch_rivals(disputed_keys, claim_ids)

    results = []
    for claim in claims:
        evidence = all_evidence.get(claim["id"], [])
        decoded = json.loads(claim["value_json"])
        text = decoded.get("old_value") if isinstance(decoded, dict) and decoded.get("_type") == "superseded_value" else decoded
        replacement = replacement_map.get(claim.get("superseded_by_id")) if claim.get("superseded_by_id") else None
        result = {
            "type": "claim",
            "id": claim["id"],
            "text": text,
            "status": claim["status"],
            "confidence": claim["confidence"],
            "valid_from": claim["valid_from"],
            "replacement": replacement,
            "evidence": evidence,
            "relations": relations_map.get(claim["id"], []),
        }
        if claim["status"] == "disputed" and claim.get("conflict_key"):
            result["conflicts"] = rivals_map.get(claim["id"], [])
        results.append(result)
    return results
```

### 新增批量方法到 `src/hl_mem/storage/repository.py`

```python
class EvidenceRepository:
    def batch_get_links_for_derived(self, derived_type: str, derived_ids: list[str]) -> dict[str, list[dict]]:
        """批量获取多个 derived_id 的 evidence links。"""
        if not derived_ids:
            return {}
        result = {did: [] for did in derived_ids}
        for start in range(0, len(derived_ids), 500):
            chunk = derived_ids[start:start+500]
            placeholders = ",".join("?" for _ in chunk)
            rows = self.connection.execute(
                f"SELECT * FROM evidence_links WHERE derived_type=? AND derived_id IN ({placeholders})",
                (derived_type, *chunk),
            ).fetchall()
            for row in rows:
                link = dict(row)
                result.setdefault(link["derived_id"], []).append(
                    {"type": link["evidence_type"], "id": link["evidence_id"]}
                )
        return result

class ClaimRepository:
    def batch_get_claims(self, claim_ids: list[str]) -> dict[str, dict[str, Any]]:
        """批量获取多个 claim。"""
        if not claim_ids:
            return {}
        result = {}
        for start in range(0, len(claim_ids), 500):
            chunk = claim_ids[start:start+500]
            placeholders = ",".join("?" for _ in chunk)
            rows = self.connection.execute(
                f"SELECT * FROM claims WHERE id IN ({placeholders})", chunk
            ).fetchall()
            for row in rows:
                claim = dict(row)
                result[claim["id"]] = claim
        return result
```

### 新增批量查询到 RecallService

```python
def _batch_evidence(self, evidence_repo, claim_ids):
    return evidence_repo.batch_get_links_for_derived("claim", claim_ids)

def _batch_replacements(self, claim_repo, superseded_ids):
    if not superseded_ids:
        return {}
    claims = claim_repo.batch_get_claims(superseded_ids)
    result = {}
    for cid, claim in claims.items():
        result[cid] = {
            "id": claim["id"],
            "text": json.loads(claim["value_json"]),
            "valid_from": claim["valid_from"],
        }
    return result

def _batch_relations(self, claim_ids):
    from hl_mem.domain.relations import get_relations_batch
    return get_relations_batch(self.connection, claim_ids)

def _batch_rivals(self, conflict_keys, exclude_ids):
    if not conflict_keys:
        return {}
    unique_keys = list(dict.fromkeys(conflict_keys))
    result = {cid: [] for cid in exclude_ids}
    # 需要知道哪个 claim 属于哪个 key
    for start in range(0, len(unique_keys), 500):
        chunk = unique_keys[start:start+500]
        placeholders = ",".join("?" for _ in chunk)
        rows = self.connection.execute(
            f"SELECT id, value_json, conflict_key FROM claims "
            f"WHERE conflict_key IN ({placeholders}) AND status='disputed'",
            chunk,
        ).fetchall()
        for row in rows:
            r = dict(row)
            # 找到对应的 claim
            for claim_id in exclude_ids:
                result.setdefault(claim_id, []).append({"id": r["id"], "value_json": r["value_json"]})
    # 这里需要更精确的映射——实际上每个 disputed claim 的 rivals 是共享 conflict_key 但不同 id 的
    # 更好的做法：返回 {conflict_key: [rivals]}
    return result
```

**注意**: `_batch_rivals` 需要更精确的实现。当前每条 disputed claim 的 rivals 是：`WHERE conflict_key=? AND status='disputed' AND id!=claim_id`。批量版应该先按 conflict_key 分组查出所有 disputed claims，然后在 Python 中按 claim 的 conflict_key 映射。

### `src/hl_mem/domain/relations.py` — 新增 batch 版

```python
def get_relations_batch(connection, claim_ids: list[str]) -> dict[str, list[dict]]:
    """批量获取多个 claim 的 relations。"""
    if not claim_ids:
        return {}
    result = {cid: [] for cid in claim_ids}
    for start in range(0, len(claim_ids), 500):
        chunk = claim_ids[start:start+500]
        placeholders = ",".join("?" for _ in chunk)
        rows = connection.execute(
            f"SELECT derived_id, relation, evidence_type, evidence_id "
            f"FROM evidence_links WHERE derived_type='claim' "
            f"AND relation IN ('supports','contradicts','follows','about') "
            f"AND derived_id IN ({placeholders})",
            chunk,
        ).fetchall()
        for row in rows:
            r = dict(row)
            result.setdefault(r["derived_id"], []).append(r)
    return result
```

## 约束
- 不要修改 tests/ 目录下的任何现有测试文件（只新建文件）
- 不要运行 pytest
- 完成后运行 `git add src/ tests/ pyproject.toml` 和 `git commit`
- 版本号 0.3.2 → 0.3.3
- hybrid_claims 和 repository 查询方法的 namespace 参数保留默认值 `"default"` 以保持向后兼容
