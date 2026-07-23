# Phase 8：3 个硬伤修复

## 项目位置
`D:/workspace/hl_agent/hl_mem/`

## 测试
```bash
.venv/Scripts/python.exe -m pytest tests/unit/ -q --tb=short
```

---

## 硬伤 1：派生记忆接入正式召回

### 现状

- `recall/observation.py` 的 `ObservationBuilder` 是死代码
- `workers/mental_models.py` 的 `DerivedMemoryMaintainer` 有完整的 rebuild/stale 逻辑但只在 worker 中被间接调用
- `application/recall.py:103` 的 `_assemble_results()` 只组装 claim，不查 derivations
- REST recall 固定返回 `observations: []`
- `derivations` 表存在（migration 007），有 `id,kind,body,status,source_watermark,proof_count,updated_at`

### 修复

#### 1a. RecallService 召回时查询活跃 derivation

在 `application/recall.py` 的 `recall()` 方法中，组装结果后查询与召回 claim 相关的活跃 derivation：

```python
def _assemble_observations(self, claim_ids: list[str]) -> list[dict[str, Any]]:
    """查询与召回 claim 相关的活跃派生记忆。"""
    if not claim_ids:
        return []
    placeholders = ",".join("?" for _ in claim_ids)
    rows = self.connection.execute(
        f"SELECT d.id,d.kind,d.body,d.confidence,d.updated_at "
        f"FROM derivations d "
        f"JOIN evidence_links e ON e.derived_id=d.id AND e.derived_type=d.kind "
        f"WHERE d.status='active' AND e.evidence_type='claim' AND e.evidence_id IN ({placeholders}) "
        f"GROUP BY d.id ORDER BY d.updated_at DESC LIMIT 10",
        claim_ids,
    ).fetchall()
    return [dict(row) for row in rows]
```

#### 1b. 在 recall() 返回中填充 observations

```python
# 替换原来的 "observations": []
observations = self._assemble_observations([claim["id"] for claim in results_claims])
return {
    "results": results,
    "observations": observations,
    ...
}
```

#### 1c. Worker 定期构建 derivation

在 `workers/worker.py` 的 `run_forever()` 定时块中，调用 `DerivedMemoryMaintainer` 扫描：

```python
# 在 next_ttl 块中添加
from hl_mem.workers.mental_models import DerivedMemoryMaintainer
DerivedMemoryMaintainer(self.connection).scan_and_build(_now())
```

如果 `DerivedMemoryMaintainer` 没有 `scan_and_build()` 方法，添加一个：扫描同 conflict_key 的 active claims，用 `ObservationBuilder` 尝试构建，成功则写入 derivations 表。

#### 1d. stale 传播

`DerivedMemoryMaintainer.mark_stale_dependencies()` 已实现，在 worker 定时块中调用即可（和 ttl/decay 一起）。

---

## 硬伤 2：disputed/candidate 终态收敛

### 现状

- claim 进入 disputed/candidate 后没有出路
- `consolidate.py` 的 LLM 判定只记录到 `consolidation_pairs` 表，不会改变 disputed 状态
- 没有 conflict case 状态机

### 修复

#### 2a. 新建 migration `013_conflict_cases.sql`

```sql
CREATE TABLE IF NOT EXISTS conflict_cases (
    id TEXT PRIMARY KEY,
    pair_key TEXT NOT NULL,
    left_claim_id TEXT NOT NULL,
    right_claim_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending/auto_resolved/manual_required/resolved/rejected
    decision TEXT,                            -- keep_left/keep_right/state_change/coexist/merged_candidate
    rationale TEXT,
    confidence REAL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY (left_claim_id) REFERENCES claims(id),
    FOREIGN KEY (right_claim_id) REFERENCES claims(id)
);

INSERT INTO schema_migrations (version, applied_at) VALUES ('013_conflict_cases', datetime('now'));
```

#### 2b. 在 `consolidate.py` 的 `run_batch()` 中创建 conflict case

当判定结果为 `contradiction` 且置信度 >= threshold 时，除了标记 disputed，还创建 conflict_case：

```python
if decision.kind == "contradiction":
    # 现有逻辑：标记 disputed
    ...
    # 新增：创建 conflict case
    self.connection.execute(
        "INSERT OR IGNORE INTO conflict_cases "
        "(id,pair_key,left_claim_id,right_claim_id,status,decision,confidence,rationale,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, pair.pair_key, pair.left["id"], pair.right["id"],
         "manual_required" if decision.confidence < 0.9 else "auto_resolved",
         None, decision.confidence, decision.rationale, datetime.now(timezone.utc).isoformat()),
    )
```

#### 2c. 自动解决低风险 conflict case

在 worker 定时块中添加自动解决：对 `auto_resolved` 状态的 case，如果两个 claim 都还是 disputed，选择 source_authority 更高的那条恢复为 active：

```python
def auto_resolve_conflicts(connection, now: str) -> dict[str, int]:
    """自动解决低风险冲突：保留权威性更高的 claim。"""
    rows = connection.execute(
        "SELECT * FROM conflict_cases WHERE status='auto_resolved' AND resolved_at IS NULL"
    ).fetchall()
    resolved = 0
    for row in rows:
        case = dict(row)
        left = ClaimRepository(connection).get_claim(case["left_claim_id"])
        right = ClaimRepository(connection).get_claim(case["right_claim_id"])
        if not left or not right:
            continue
        # 选择权威性更高的
        left_authority = {"high": 3, "medium": 2, "low": 1}.get(left.get("source_authority", "medium"), 2)
        right_authority = {"high": 3, "medium": 2, "low": 1}.get(right.get("source_authority", "medium"), 2)
        winner_id = case["left_claim_id"] if left_authority >= right_authority else case["right_claim_id"]
        loser_id = case["right_claim_id"] if winner_id == case["left_claim_id"] else case["left_claim_id"]
        # 胜者恢复 active，败者保持 disputed（不删除）
        connection.execute("UPDATE claims SET status='active' WHERE id=? AND status='disputed'", (winner_id,))
        connection.execute("UPDATE conflict_cases SET status='resolved',resolved_at=?,decision=? WHERE id=?",
                          (now, f"keep_{'left' if winner_id == case['left_claim_id'] else 'right'}", case["id"]))
        resolved += 1
    connection.commit()
    return {"auto_resolved": resolved}
```

#### 2d. CLI 审核命令

在 `cli.py` 中添加 `conflicts` 子命令：

```python
# python -m hl_mem conflicts list    — 列出 pending/manual_required 的 case
# python -m hl_mem conflicts resolve <case_id> keep_left|keep_right|coexist|reject
```

#### 2e. disputed 召回按意图返回"冲突包"

在 `_assemble_results()` 中，如果 claim status 是 disputed，附带冲突对手信息：

```python
if claim["status"] == "disputed":
    # 查找同 conflict_key 的其他 disputed claim
    rivals = self.connection.execute(
        "SELECT id,value_json FROM claims WHERE conflict_key=? AND status='disputed' AND id!=?",
        (claim.get("conflict_key"), claim["id"]),
    ).fetchall()
    result["conflicts"] = [dict(r) for r in rivals]
```

---

## 硬伤 3：召回上下文预算 + 跨类型组装

### 现状

- `budget_pack()` 已实现在 `recall/extended_pipeline.py` 但未接入
- `limit=20` 是条数限制，不是 token 预算
- 没有跨类型（claim + observation + policy）组装

### 修复

#### 3a. RecallInput 增加可选参数

在 `api/schemas.py` 的 `RecallInput` 中添加：

```python
token_budget: int | None = None  # None = 不做预算控制
context_mode: str | None = None   # None = 默认列表模式, "packed" = 上下文打包
```

#### 3b. RecallService 支持 context 打包

当 `context_mode == "packed"` 时：

```python
def _assemble_context(self, claims: list, observations: list, policies: list, token_budget: int) -> dict:
    """跨类型组装上下文，受 token 预算控制。"""
    items = []
    # 按优先级：claim > observation > policy
    # 类型配额：至少 2 条 policy，至少 1 条 observation（如果有）
    all_items = (
        [{"type": "claim", "data": c, "priority": 2} for c in claims]
        + [{"type": "observation", "data": o, "priority": 1} for o in observations]
        + [{"type": "policy", "data": p, "priority": 0} for p in policies]
    )
    # 按 priority 排序
    all_items.sort(key=lambda x: -x["priority"])
    # token 估算：粗略中文 token = len(text) / 2
    packed = []
    used = 0
    for item in all_items:
        text = str(item["data"].get("text") or item["data"].get("body") or item["data"].get("procedure") or "")
        cost = max(1, (len(text) + 1) // 2)
        if packed and used + cost > token_budget:
            continue
        packed.append(item)
        used += cost
        if used >= token_budget:
            break
    return {
        "context_items": packed,
        "used_tokens_estimate": used,
        "truncated": used >= token_budget,
    }
```

#### 3c. recall() 返回 context（当 context_mode="packed"）

```python
if payload.context_mode == "packed":
    budget = payload.token_budget or 2000
    context = self._assemble_context(results, observations, policies, budget)
    return {"context": context, "results": results, ...}
```

---

## 约束

1. 不要运行 pytest
2. 不要修改 tests/ 目录下的任何文件
3. 向后兼容：现有 180 个测试必须全部通过
4. 不要新增依赖
5. 不要问任何问题
6. 完成后 `git add -A && git commit -m "feat: fix 3 hard issues — observation recall, conflict resolution, context budget"`
