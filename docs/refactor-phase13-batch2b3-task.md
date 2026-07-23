# Batch 2B + 3 — P1-4 预算硬上限 + P1-5 API 体积限制

## P1-4: 上下文预算可超限

### 文件: `src/hl_mem/application/recall.py`

### 改动: `_assemble_context()` 静态方法

**当前问题** (约第 108 行): `if packed and used + cost > token_budget` —— 第一条超过预算时 packed 为空跳过检查。

**共识方案**: 无条件检查，超限直接跳过（包括第一条）。允许返回空 context。不修改原始 data 字段。

```python
@staticmethod
def _assemble_context(claims, observations, policies, token_budget):
    all_items = (
        [{"type": "claim", "data": item, "priority": 2} for item in claims]
        + [{"type": "observation", "data": item, "priority": 1} for item in observations]
        + [{"type": "policy", "data": item, "priority": 0} for item in policies]
    )
    all_items.sort(key=lambda item: -item["priority"])
    packed = []
    used = 0
    truncated = False
    for item in all_items:
        data = item["data"]
        text = str(data.get("text") or data.get("body") or data.get("procedure") or "")
        cost = max(1, (len(text) + 1) // 2)
        if used + cost > token_budget:  # 无条件检查，不再判断 packed 是否为空
            truncated = True
            continue
        packed.append(item)
        used += cost
        if used >= token_budget:
            truncated = len(packed) < len(all_items)
            break
    return {"context_items": packed, "used_tokens_estimate": used, "truncated": truncated}
```

---

## P1-5: API 无输入体积上限

### 文件 1: `src/hl_mem/api/schemas.py`

给主要字段加 `max_length`：

```python
class EventInput(BaseModel):
    id: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=200)
    tenant_id: str = Field(default="default", max_length=100)
    user_id: str | None = Field(default=None, max_length=100)
    project_id: str | None = Field(default=None, max_length=100)
    agent_id: str | None = Field(default=None, max_length=100)
    session_id: str | None = Field(default=None, max_length=200)
    event_type: str = Field(default="message", max_length=50)
    actor_type: str = Field(default="user", max_length=50)
    actor_id: str | None = Field(default=None, max_length=100)
    content: dict[str, Any] | str = Field(default_factory=dict)
    occurred_at: str | None = None
    source_uri: str | None = Field(default=None, max_length=2000)
    sensitivity: str = Field(default="normal", max_length=20)

class RecallInput(BaseModel):
    query: str = Field(max_length=2000)
    # ... 其余字段加 namespace, max_length 同理

class MemoryInput(BaseModel):
    text: str | None = Field(default=None, max_length=50000)
    content: str | None = Field(default=None, max_length=50000)
    subject: str = Field(default="用户", max_length=200)
    predicate: str = Field(default="explicit_memory", max_length=100)
    qualifiers: dict[str, Any] = Field(default_factory=dict)

class EpisodeInput(BaseModel):
    goal: str = Field(min_length=1, max_length=5000)
    session_id: str | None = Field(default=None, max_length=200)
    task_type: str | None = Field(default=None, max_length=50)

class TraceInput(BaseModel):
    action: str = Field(min_length=1, max_length=10000)
    observation: str | None = Field(default=None, max_length=50000)
    error_signature: str | None = Field(default=None, max_length=500)
    value: float = 0.0

class FeedbackInput(BaseModel):
    query_id: str = Field(min_length=1, max_length=200)
    memory_id: str = Field(min_length=1, max_length=200)
    helpful: bool
    task_outcome: str | None = Field(default=None, max_length=5000)
```

### 文件 2: `src/hl_mem/api/server.py`

新增 ASGI middleware 限制请求体大小：

```python
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

MAX_REQUEST_BODY = int(os.getenv("HL_MEM_MAX_REQUEST_BODY", str(2 * 1024 * 1024)))  # 2MB 默认

class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """拒绝超过指定大小的请求体，返回 413。"""
    async def dispatch(self, request, call_next):
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_REQUEST_BODY:
            return Response(status_code=413, content="Request body too large")
        return await call_next(request)

# 在 create_app() 中注册（在 app 创建之后、路由注册之前）
app.add_middleware(RequestSizeLimitMiddleware)
```

### 文件 3: `src/hl_mem/settings.py`

加 `max_request_body` 字段：

```python
@dataclass(frozen=True)
class Settings:
    # ... 现有字段 ...
    max_request_body: int = 2 * 1024 * 1024  # 2MB
```

## 约束
- 不要修改 tests/ 目录下的任何现有测试文件
- 不要运行 pytest
- 完成后运行 `git add src/ pyproject.toml` 和 `git commit`
- 版本号 0.3.3 → 0.3.4
