"""API 请求模型。集中定义事件、召回、记忆、Episode 与反馈接口的 Pydantic DTO。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from hl_mem.config import RECALL_DEFAULT_LIMIT
from hl_mem.recall.policy import RecallIntent


class EventInput(BaseModel):
    """事件写入请求。"""

    id: str | None = None
    idempotency_key: str | None = None
    tenant_id: str = "default"
    user_id: str | None = None
    project_id: str | None = None
    agent_id: str | None = None
    session_id: str | None = None
    event_type: str = "message"
    actor_type: str = "user"
    actor_id: str | None = None
    content: dict[str, Any] | str = Field(default_factory=dict)
    occurred_at: str | None = None
    source_uri: str | None = None
    sensitivity: str = "normal"


class RecallInput(BaseModel):
    """记忆召回请求。"""

    query: str
    limit: int = Field(default=RECALL_DEFAULT_LIMIT, ge=1, le=100)
    as_of: str | None = None
    session_id: str | None = None
    intent: RecallIntent | None = None
    known_as_of: str | None = None
    token_budget: int | None = Field(default=None, ge=1)
    context_mode: str | None = Field(default=None, pattern="^(packed)$")


class MemoryInput(BaseModel):
    """显式记忆写入请求。"""

    text: str | None = None
    content: str | None = None
    subject: str = "用户"
    predicate: str = "explicit_memory"
    qualifiers: dict[str, Any] = Field(default_factory=dict)


class EpisodeInput(BaseModel):
    """创建 Episode 的请求。"""

    goal: str = Field(min_length=1)
    session_id: str | None = None
    task_type: str | None = None


class TraceInput(BaseModel):
    """追加 Episode Trace 的请求。"""

    action: str = Field(min_length=1)
    observation: str | None = None
    error_signature: str | None = None
    value: float = 0.0


class EpisodeUpdate(BaseModel):
    """更新 Episode 结果的请求。"""

    status: str | None = None
    reward: float | None = Field(default=None, ge=0.0, le=1.0)
    outcome_summary: str | None = None


class FeedbackInput(BaseModel):
    """检索结果反馈请求。"""

    query_id: str = Field(min_length=1)
    memory_id: str = Field(min_length=1)
    helpful: bool
    task_outcome: str | None = None
