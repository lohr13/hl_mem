# Phase 15 Batch 3: 拆分三个上帝函数

## 概述

修复 P1-3：三条核心路径的大型编排函数承担过多职责。

---

## 问题

三个函数各承担 8+ 职责，新增功能需要进入高分支密度函数：

| 函数 | 位置 | 行数 | 职责数 |
|------|------|------|--------|
| `store_extracted()` | `application/ingest.py:155-359` | ~204 | 规范化、TTL、向量化、精确去重、语义去重、冲突处理、状态迁移、证据写入、审计、事务 |
| `hybrid_claims()` | `recall/recall_pipeline.py:96-351` | ~255 | 两路检索、兼容降级、可见性、RRF、多因子排序、关系扩展、rerank、trace、审计 |
| `Worker._dispatch()` | `workers/worker.py:106-182` + `:184-300` | ~176 | 调度、维护、7类job分派、事件提取 |

---

## 修复方案

**原则：不引入新框架或 DI 容器，只提取同文件/同包内的纯阶段函数。**

### 3.1 store_extracted() 拆分

拆为同文件内或同包内的阶段函数：

```python
# application/ingest.py

def store_extracted(self, extraction_result, source_event, settings):
    """Orchestrator — 只做阶段协调和事务边界，不含具体逻辑。"""
    claim_drafts = _build_claim_drafts(extraction_result, source_event, settings)
    resolution = _find_resolution(claim_drafts, self.repo, self.embedder, settings)
    return _persist_resolution(resolution, self.repo, source_event, settings)

def _build_claim_drafts(extraction_result, source_event, settings):
    """阶段1: 规范化 + TTL 计算"""
    ...

def _find_resolution(drafts, repo, embedder, settings):
    """阶段2: 向量化 → 精确去重 → 语义去重 → 冲突处理"""
    ...

def _persist_resolution(resolution, repo, source_event, settings):
    """阶段3: 状态迁移 + 证据写入 + 审计 (在事务内)"""
    ...
```

### 3.2 hybrid_claims() 拆分

```python
# recall/recall_pipeline.py

def hybrid_claims(self, query, settings, debug=False):
    """Orchestrator — 协调检索流程。"""
    candidates = self._collect_candidates(query, settings)
    filtered = self._filter_and_score(candidates, query, settings)
    expanded = self._expand_related(filtered, settings)
    reranked = self._rerank(expanded, query, settings)
    return self._finalize(reranked, query, debug, settings)

def _collect_candidates(self, query, settings):
    """阶段1: 两路检索（FTS + 向量）"""
    ...

def _filter_and_score(self, candidates, query, settings):
    """阶段2: 可见性过滤 + RRF 融合 + 多因子排序"""
    ...

def _expand_related(self, scored, settings):
    """阶段3: 关系扩展（如果启用）"""
    ...

def _rerank(self, expanded, query, settings):
    """阶段4: reranker 重排"""
    ...

def _finalize(self, reranked, query, debug, settings):
    """阶段5: trace 组装 + 审计"""
    ...
```

### 3.3 Worker._dispatch() 拆分

```python
# workers/worker.py

# 用注册表替代大型 if-elif 分派
JOB_HANDLERS: dict[str, Callable] = {
    "extract": _handle_extract,
    "consolidate": _handle_consolidate,
    "reclassify": _handle_reclassify,
    "induce_policies": _handle_induce_policies,
    "decay": _handle_decay,
    "archive": _handle_archive,
    "cleanup": _handle_cleanup,
}

def _dispatch(self, job):
    """Orchestrator — 查表分派。"""
    handler = JOB_HANDLERS.get(job["job_type"])
    if handler:
        return handler(self, job)
    raise ValueError(f"Unknown job type: {job['job_type']}")

def _run_maintenance(self, now):
    """维护循环收敛为单一方法。"""
    ...
```

---

## 约束

1. **不要修改 tests/ 目录下的任何文件**（测试 monkeypatch 断言更新由 Hermes 负责）
2. **不要运行 pytest**
3. **完成后运行**：`git add src/ && git commit -m "refactor(orchestration): split three god functions into focused stage functions"`
4. **不要用 `git add -A`**
5. **提取的函数使用 `_` 前缀（私有），保持包内可见**
6. **保持现有事务边界不变**（`store_extracted` 的 `BEGIN IMMEDIATE` 不能丢）
7. **保持函数签名不变**（对外接口 `store_extracted()` / `hybrid_claims()` / `_dispatch()` 签名不变，只拆内部）
8. **如果测试 monkeypatch 了被移动的内部函数**：在原位置保留 re-export（`_build_observation` 模式），以避免 AttributeError
