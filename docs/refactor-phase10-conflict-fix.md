# Phase 10：ConflictResolver 假冲突根因修复

## 问题背景

hl_mem 的 `ConflictResolver.resolve()` 存在系统性误判，导致大量 claim 被错误标记为 disputed/candidate。

### 现状数据

- disputed: 222 条（其中 136 条来自 `.other` 通配符槽位）
- candidate: 93 条（其中 47 条来自 `.other`）
- 根因：`fact.other` / `plan.other` / `config.other` 等通配符槽位下，不同事实共享同一 conflict_key，值不同时被判为 "contradicts"

### 三个 bug 的精确位置

**Bug 1：`.other` 通配符不豁免**（`conflict.py:91-93`）

```python
# 当前代码
if existing.get("source_authority", "medium") == new.get("source_authority", "medium"):
    return "contradicts"
return "uncertain"
```

`.other` 是兜底槽位（如 `fact.other`、`config.other`），明确表示"不够具体到能判断冲突"。当 slot 是 `.other` 时，不同值不应该是矛盾——它们只是不同的独立事实。

**Bug 2：predicate 中英文不匹配**（`conflict.py:89`）

```python
# 当前代码
if new.get("predicate") in {"preference", "service_status"}:
    return "state_change"
```

但数据库里存的 predicate 是中文归一化后的值（"偏好"、"状态"），不是英文。所以健康状态变更永远不会被识别为 `state_change`，全部掉到 `contradicts`。

**Bug 3：ingest 路径的 disputed 不创建 conflict_case**（`ingest.py:234-237`）

```python
elif resolution == "contradicts":
    claim["status"] = "disputed"
elif resolution == "uncertain":
    claim["status"] = "candidate"
```

ingest 管线直接标 disputed/candidate，但不创建 `conflict_cases` 记录。Phase 8 的 `auto_resolve_conflicts()` 只扫 `conflict_cases` 表，所以这些 claim 永远不会被自动解决。

### 需要你做的

1. **分析三个 bug 的根因**，确认上述判断是否准确
2. **提出修复方案**（每个 bug 的具体改法、函数签名变化、影响面分析）
3. **检查是否有遗漏**（是否还有其他路径会产生假冲突）
4. **不要实现**，只输出方案文档
5. **特别注意**：向后兼容，180 个现有测试不能断
