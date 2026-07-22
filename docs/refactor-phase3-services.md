# Phase 3：建立共享应用服务层

## 背景

Codex 审查发现 P1-3（worker 依赖 api.pipeline）、P1-6（server.py 过载）、P1-9（三套召回/删除语义）。核心问题是 REST、MCP、Worker 三个入口各自实现业务逻辑，没有共享的应用服务。

## 项目位置

`D:/workspace/hl_agent/hl_mem/`

## 测试运行方式

```bash
.venv/Scripts/python.exe -m pytest tests/unit/ -q --tb=short
```

---

## 目标结构

新建 `src/hl_mem/application/` 包，包含三个应用服务：

```
application/
├── __init__.py
├── ingest.py      # IngestService — 事件写入 + 记忆保存 + 提取管线
├── recall.py      # RecallService — 混合召回 + 访问记录 + 反馈
└── forget.py      # ForgetService — 撤回记忆 + 清除向量 + stale 传播
```

三个入口（REST server.py、MCP server.py、Worker worker.py）都只做适配层，委托给这些服务。

---

## 修改 1：IngestService (`application/ingest.py`)

### 从哪里提取逻辑

- `api/server.py:189-247` — `post_event()` 中的 event 写入 + `_queue_event()`
- `api/server.py:432-475` — `save_memory()` 中的 explicit_memory 写入
- `api/pipeline.py:42-249` — `store_extracted()` + `compute_fact_hash()` + `claim_text()` + `_link_event()`

### API 设计

```python
class IngestService:
    """记忆写入应用服务，拥有事务边界。"""

    def __init__(self, connection: Any, embedder: Any) -> None:
        self.connection = connection
        self.embedder = embedder

    def ingest_event(
        self,
        event: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """写入事件并创建提取任务。返回 {id, created}。"""
        # 逻辑来自 server.py post_event()

    def save_explicit_memory(
        self,
        text: str,
        subject: str = "用户",
        predicate: str = "explicit_memory",
        qualifiers: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """写入显式记忆事件并排队。返回 {id}。"""
        # 逻辑来自 server.py save_memory()

    @staticmethod
    def store_extracted(
        connection: Any,
        extracted: Any,
        event: dict[str, Any],
        now: str,
        embedder: Any,
        authority: str | None = None,
        ttl_days: int = 7,
    ) -> str:
        """提取的 claim 写入存储（含去重、冲突、嵌入）。

        保持为 @staticmethod 因为 worker.py 直接调用它。
        """
        # 逻辑来自 pipeline.py store_extracted()
```

### 实现要点

1. `ingest_event()` 和 `save_explicit_memory()` 内部管理 `BEGIN IMMEDIATE` → commit/rollback
2. `_queue_event()` 成为 IngestService 的私有方法
3. `store_extracted()` 保持 `@staticmethod`，因为 Worker 直接调用它且参数签名已经稳定
4. `compute_fact_hash()`、`claim_text()`、`_link_event()` 移到 `application/ingest.py` 或保留在 pipeline.py 作为公共工具（保留 pipeline.py 作为纯函数模块，只删除 `store_extracted`）

**关键：为了向后兼容，`api/pipeline.py` 保留对 `store_extracted` 的 re-export：**
```python
# api/pipeline.py — 向后兼容 re-export
from hl_mem.application.ingest import IngestService
store_extracted = IngestService.store_extracted
```

---

## 修改 2：RecallService (`application/recall.py`)

### 从哪里提取逻辑

- `api/server.py:249-349` — `recall()` 中的混合召回 + access recording + feedback recording + evidence assembly + replacement lookup + policy matching

### API 设计

```python
class RecallService:
    """记忆召回应用服务。"""

    def __init__(self, connection: Any, embedder: Any, reranker: Any = None) -> None:
        self.connection = connection
        self.embedder = embedder
        self.reranker = reranker

    def recall(
        self,
        query: str,
        limit: int = 20,
        as_of: str | None = None,
        intent: str | None = None,
        known_as_of: str | None = None,
    ) -> dict[str, Any]:
        """执行混合召回，返回 {results, observations, policies, total, query_id}。

        包含：hybrid_claims → record_access → feedback_record → evidence assembly → replacement lookup
        """
        # 逻辑来自 server.py recall()
```

### 实现要点

1. `recall()` 方法内部生成 query_id（或由调用方传入）
2. record_access 和 feedback_record 的失败不中断召回（保持现有的 try/except 模式）
3. observation 固定返回空列表（`_build_observation` 确认为死代码，P1-10 后续处理）
4. policy matching 通过 ExperienceService

---

## 修改 3：ForgetService (`application/forget.py`)

### 从哪里提取逻辑

- `api/server.py:477-484` — `forget()` 中的 retract + stale_observations

### API 设计

```python
class ForgetService:
    """记忆撤回应用服务。"""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def forget(self, memory_id: str) -> dict[str, Any]:
        """撤回 claim，清除 embedding，传播 stale 标记。

        返回 {id, forgotten}。
        如果 claim 不存在抛 ValueError。
        """
        # 逻辑来自 server.py forget()
```

### 实现要点

1. 内部使用 lifecycle `assert_transition()` 验证状态转换
2. 清除 embedding（embedding_dense=NULL, embedding_sparse=NULL）
3. 调用 `stale_observations()` 传播 stale 标记
4. 事务原子化

---

## 修改 4：适配层更新

### server.py

- `post_event()` → 委托 `IngestService.ingest_event()`
- `save_memory()` → 委托 `IngestService.save_explicit_memory()`
- `recall()` → 委托 `RecallService.recall()`
- `forget()` → 委托 `ForgetService.forget()`
- 保留 FastAPI 特有的逻辑（HTTP 状态码、Header 解析、Pydantic 验证、audit emit）
- 工厂方法 `_make_embedder()` / `_make_reranker()` 暂时保留在 server.py（Phase 5 集中）

### mcp/server.py

- `memory_save` → 委托 `IngestService.save_explicit_memory()`
- `memory_recall` → 委托 `RecallService.recall()` 或 `ClaimRepository.search_claims_fts()`（如果 RecallService 太重）
- `memory_forget` → 委托 `ForgetService.forget()`

### worker.py

- `_extract()` 中的 `store_extracted()` 调用 → 改为 `IngestService.store_extracted()`（或保持导入 pipeline.py 的 re-export）

---

## 修改 5：api/pipeline.py 精简

将 `store_extracted()`、`compute_fact_hash()`、`claim_text()`、`new_id()`、`_link_event()` 的**实现**移到 `application/ingest.py`。

`api/pipeline.py` 保留 re-export 以向后兼容：
```python
"""向后兼容 re-export — 实现已迁移到 application.ingest。"""
from hl_mem.application.ingest import IngestService
store_extracted = IngestService.store_extracted
# 其他公共函数也 re-export
from hl_mem.application.ingest import compute_fact_hash, claim_text, new_id
```

---

## 约束

1. **不要运行 pytest**
2. **不要修改 tests/ 目录下的任何文件**
3. **向后兼容**：现有 180 个测试必须全部通过
4. **不要新增依赖**
5. **不要问任何问题**
6. **向后兼容 re-export 必须**：现有 `from hl_mem.api.pipeline import store_extracted` 的 import 不能断
7. 完成后 `git add -A && git commit -m "refactor(architecture): shared application services — IngestService, RecallService, ForgetService"`
