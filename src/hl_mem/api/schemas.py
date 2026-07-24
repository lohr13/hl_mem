"""API 请求模型。集中定义事件、召回、记忆、Episode 与反馈接口的 Pydantic DTO。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from hl_mem.config import RECALL_DEFAULT_LIMIT
from hl_mem.domain.recall import RecallIntent


class EventInput(BaseModel):
    """事件写入请求。"""

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
    """记忆召回请求。"""

    query: str = Field(max_length=2000)
    limit: int = Field(default=RECALL_DEFAULT_LIMIT, ge=1, le=100)
    as_of: str | None = None
    session_id: str | None = Field(default=None, max_length=200)
    intent: RecallIntent | None = None
    known_as_of: str | None = None
    token_budget: int | None = Field(default=None, ge=1)
    context_mode: str | None = Field(default=None, pattern="^(packed)$")
    namespace: str = Field(default="default", max_length=100)
    debug: bool = False


class MemoryInput(BaseModel):
    """显式记忆写入请求。"""

    text: str | None = Field(default=None, max_length=50000)
    content: str | None = Field(default=None, max_length=50000)
    subject: str = Field(default="用户", max_length=200)
    predicate: str = Field(default="explicit_memory", max_length=100)
    qualifiers: dict[str, Any] = Field(default_factory=dict)


class EpisodeInput(BaseModel):
    """创建 Episode 的请求。"""

    goal: str = Field(min_length=1, max_length=5000)
    session_id: str | None = Field(default=None, max_length=200)
    task_type: str | None = Field(default=None, max_length=50)


class TraceInput(BaseModel):
    """追加 Episode Trace 的请求。"""

    action: str = Field(min_length=1, max_length=10000)
    observation: str | None = Field(default=None, max_length=50000)
    error_signature: str | None = Field(default=None, max_length=500)
    value: float = 0.0


class EpisodeUpdate(BaseModel):
    """更新 Episode 结果的请求。"""

    status: str | None = None
    reward: float | None = Field(default=None, ge=0.0, le=1.0)
    outcome_summary: str | None = None


class FeedbackInput(BaseModel):
    """检索结果反馈请求。"""

    query_id: str = Field(min_length=1, max_length=200)
    memory_id: str = Field(min_length=1, max_length=200)
    helpful: bool
    task_outcome: str | None = Field(default=None, max_length=5000)
