# Phase 7：P2 代码质量优化

## 背景

6 个未修复的 P2 问题一次性处理。

## 项目位置

`D:/workspace/hl_agent/hl_mem/`

---

## P2-1：核心接口引入 Protocol 和 TypedDict

### 问题

145 处 `dict[str, Any]`，字段拼写错误只能运行时发现。

### 修复（针对性，不做全量）

不做全量 TypedDict 替换（成本太高）。只做两件事：

1. **新建 `src/hl_mem/protocols.py`**，定义三个核心接口协议：
   ```python
   from __future__ import annotations
   from typing import Any, Protocol

   class EmbedderProtocol(Protocol):
       dim: int
       model: str
       def embed_one(self, text: str) -> bytes: ...
       def embed_batch(self, texts: list[str]) -> list[bytes]: ...

   class ExtractorProtocol(Protocol):
       def extract(self, content: dict[str, Any], context: dict[str, Any] | None = None) -> list[Any]: ...

   class RerankerProtocol(Protocol):
       def rerank(self, query: str, documents: list[str], top_n: int = 20) -> list[tuple[int, float]]: ...
   ```

2. **在 `application/ingest.py` 的 `store_extracted()` 参数类型**从 `Any` 改为 `EmbedderProtocol`

3. **在 `application/recall.py` 的 `RecallService.__init__` 参数类型**从 `Any` 改为 `EmbedderProtocol | Any` 和 `RerankerProtocol | None`

---

## P2-2：应用异常族

### 问题

混用 ValueError、RuntimeError、InvalidStateTransitionError，API 层需要重复映射。

### 修复

1. **在 `lifecycle.py` 或新建 `src/hl_mem/errors.py`** 中定义：
   ```python
   class HlMemError(Exception):
       """hl_mem 应用异常基类。"""

   class NotFoundError(HlMemError):
       """资源不存在。"""

   class ValidationError(HlMemError):
       """输入验证失败。"""

   class ConflictError(HlMemError):
       """状态冲突（如非法状态转换）。"""

   class ConfigurationError(HlMemError):
       """配置错误（如生产环境缺 key）。"""

   class ExternalServiceError(HlMemError):
       """外部服务调用失败（如 embedding API）。"""
   ```

2. **更新关键使用点**（不要求全量替换，只改最明显的几处）：
   - `ForgetService.forget()` 中 "memory not found" → 抛 `NotFoundError`
   - `update_status()` 中无效状态 → 抛 `ValidationError`
   - production 缺 key → 抛 `ConfigurationError`
   - `InvalidTransitionError` 改为继承 `ConflictError`（保持向后兼容）

3. **server.py 的 error handler** 注册全局异常映射：
   ```python
   @app.exception_handler(NotFoundError)
   async def not_found_handler(request, exc):
       return JSONResponse(status_code=404, content={"detail": str(exc)})

   @app.exception_handler(ValidationError)
   async def validation_error_handler(request, exc):
       return JSONResponse(status_code=422, content={"detail": str(exc)})

   @app.exception_handler(ConflictError)
   async def conflict_handler(request, exc):
       return JSONResponse(status_code=409, content={"detail": str(exc)})
   ```

---

## P2-3：统一 HTTP retry policy

### 问题

- `ingest/embeddings.py`：区分可重试状态，指数退避
- `ingest/llm_extractor.py`：对所有 HTTPError 重试
- `recall/reranker.py`：不重试，吞掉全部异常
- `workers/consolidate.py`：独立策略

### 修复

1. **在 `src/hl_mem/http_utils.py`（新建）中定义统一的 retry 函数**：
   ```python
   """统一的 HTTP 重试策略。"""
   from __future__ import annotations
   import time
   import httpx

   def retry_http(
       fn: callable,
       max_attempts: int = 3,
       base_delay: float = 0.5,
       backoff_factor: float = 2.0,
   ) -> Any:
       """对 httpx 调用执行指数退避重试。

       可重试条件：TimeoutException、5xx、429。
       不重试：4xx（非 429）、ValueError、TypeError。
       """
       for attempt in range(1, max_attempts + 1):
           try:
               return fn()
           except (httpx.TimeoutException, httpx.HTTPStatusError) as error:
               is_retryable = isinstance(error, httpx.TimeoutException) or (
                   isinstance(error, httpx.HTTPStatusError)
                   and (error.response.status_code >= 500 or error.response.status_code == 429)
               )
               if not is_retryable or attempt == max_attempts:
                   raise
               time.sleep(base_delay * (backoff_factor ** (attempt - 1)))
   ```

2. **更新 embeddings.py 使用 retry_http()**（替代内联的 retry 循环）
3. **更新 llm_extractor.py 使用 retry_http()**
4. **reranker.py 保持不重试但**记录结构化失败原因（替代 bare except）

---

## P2-5：标记 schema 僵尸字段

### 问题

4 个从未在 Python 代码中引用的字段：`refresh_after`、`generated_by_model`、`prompt_version`、`refresh_policy`

### 修复（不做 migration 删除，只标记）

在 migration 001 中（或新建 migration 013）加注释标记：
```sql
-- The following fields are reserved but not currently used by application code:
-- refresh_after, generated_by_model, prompt_version, refresh_policy
-- They may be populated by future features (derived memory, model versioning).
-- Do NOT rely on their values.
```

---

## P2-6：修复 submit_retrieval_feedback 返回值

### 问题

`experience/service.py` 的 `submit_retrieval_feedback()` 在找不到曝光记录时创建一条反馈，但返回 `cursor.rowcount == 1`（基于 UPDATE 的 rowcount，而非 insert），返回值不准确。

### 修复

改为返回结构化结果：
```python
def submit_retrieval_feedback(self, ...) -> dict[str, bool]:
    """回填或创建反馈。返回 {created, updated}。"""
    cursor = self.connection.execute("UPDATE retrieval_feedback SET ...")
    if cursor.rowcount > 0:
        return {"created": False, "updated": True}
    # 没找到曝光记录，创建独立反馈
    self.connection.execute("INSERT INTO retrieval_feedback ...")
    return {"created": True, "updated": False}
```

同步更新 `api/server.py` 中调用此方法的端点返回值（向后兼容：返回 dict 而非 bool）。

---

## P2-8：合并 router.py 和 policy.py

### 问题

`recall/router.py`（28行）和 `recall/policy.py`（18行后因 Phase 4 提取大部分到 domain/temporal.py 更少了）都做 recall intent 路由。

### 修复

把 `router.py` 的 `route_query()` 和 `QueryRoute` 合并到 `policy.py`（因为 policy.py 已是 recall intent 的归属地）。或者反过来：把 policy.py 剩余代码合并到 router.py。

**选择**：保留 `policy.py`，把 `router.py` 的内容合并进去，`router.py` 改为 re-export 向后兼容。

---

## 约束

1. **不要运行 pytest**
2. **不要修改 tests/ 目录下的任何文件**
3. **向后兼容**：现有 180 个测试必须全部通过
4. **不要新增依赖**
5. **不要问任何问题**
6. 完成后 `git add -A && git commit -m "refactor(quality): P2 fixes — protocols, errors, retry, feedback, router merge, zombie fields"`
