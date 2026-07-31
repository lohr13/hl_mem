"""Context Packet v1 的无状态组装与 exposure 物化。"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from hl_mem.experience.service import ExperienceService

LOGGER = logging.getLogger(__name__)

Answerability = Literal["supported", "low_confidence", "no_evidence"]
FeedbackState = Literal["available", "degraded"]
MemoryType = Literal["claim", "observation", "policy", "episode", "trace"]

_ANSWERABILITY_VALUES = frozenset({"supported", "low_confidence", "no_evidence"})
_MEMORY_TYPE_VALUES = frozenset({"claim", "observation", "policy", "episode", "trace"})


def estimate_tokens(text: str) -> int:
    """沿用 recall v1 的可复现粗略 token 估算。"""
    return max(1, (len(text) + 1) // 2)


@dataclass(frozen=True, slots=True)
class RetrievalBundleItem:
    """可缓存、无 receipt 的单条最终候选。"""

    type: MemoryType
    id: str
    text: str
    evidence: tuple[Mapping[str, Any], ...] = ()
    score: float | None = None

    def __post_init__(self) -> None:
        if self.type not in _MEMORY_TYPE_VALUES:
            raise ValueError(f"unsupported memory type: {self.type}")
        if not self.id:
            raise ValueError("retrieval bundle item id must be non-empty")
        if not isinstance(self.text, str):
            raise TypeError("retrieval bundle item text must be a string")


@dataclass(frozen=True, slots=True)
class RetrievalBundle:
    """可缓存的有序检索结果，不包含 feedback_id 或 delivery receipt。"""

    query_id: str
    answerability: Answerability
    items: tuple[RetrievalBundleItem, ...]
    used_tokens_estimate: int | None = None
    truncated: bool | None = None

    def __post_init__(self) -> None:
        if not self.query_id:
            raise ValueError("query_id must be non-empty")
        if self.answerability not in _ANSWERABILITY_VALUES:
            raise ValueError(f"unsupported answerability: {self.answerability}")
        if self.used_tokens_estimate is not None and self.used_tokens_estimate < 0:
            raise ValueError("used_tokens_estimate must be non-negative")


def pack_retrieval_items(
    items: Iterable[RetrievalBundleItem],
    token_budget: int,
) -> tuple[tuple[RetrievalBundleItem, ...], int, bool]:
    """按输入顺序裁剪 item，并返回稳定的 token 与截断诊断。"""
    candidates = tuple(items)
    if token_budget < 1:
        return (), 0, bool(candidates)
    packed: list[RetrievalBundleItem] = []
    used = 0
    for item in candidates:
        cost = estimate_tokens(item.text)
        if used + cost > token_budget:
            continue
        packed.append(item)
        used += cost
        if used >= token_budget:
            break
    return tuple(packed), used, len(packed) < len(candidates)


def pack_retrieval_bundle(
    bundle: RetrievalBundle,
    token_budget: int,
) -> RetrievalBundle:
    """对 receipt-free bundle 做最终预算裁剪，保留原始 query_id。"""
    packed, used, truncated = pack_retrieval_items(bundle.items, token_budget)
    return RetrievalBundle(
        query_id=bundle.query_id,
        answerability=bundle.answerability,
        items=packed,
        used_tokens_estimate=used,
        truncated=truncated,
    )


class ContextPacketAssembler:
    """为一次最终注入生成新 receipt，并组装严格 Context Packet v1。"""

    def __init__(
        self,
        target: sqlite3.Connection | ExperienceService,
        *,
        feedback_id_factory: Callable[[], str] | None = None,
        clock: Callable[[], str] | None = None,
        persist_exposures: Callable[[list[tuple[Any, ...]]], int] | None = None,
    ) -> None:
        self.service = target if isinstance(target, ExperienceService) else ExperienceService(target)
        self.feedback_id_factory = feedback_id_factory or (lambda: uuid.uuid4().hex)
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
        self.persist_exposures = persist_exposures or self.service.record_exposure_batch
        self.last_error: Exception | None = None

    @staticmethod
    def make_bundle(
        query_id: str,
        answerability: Answerability,
        items: Iterable[RetrievalBundleItem],
        token_budget: int | None = None,
    ) -> RetrievalBundle:
        """从有序候选创建 receipt-free RetrievalBundle，并可选冻结预算。"""
        bundle = RetrievalBundle(query_id, answerability, tuple(items))
        return pack_retrieval_bundle(bundle, token_budget) if token_budget is not None else bundle

    def assemble(
        self,
        bundle: RetrievalBundle,
        token_budget: int | None = None,
    ) -> dict[str, Any]:
        """物化 exposure；失败时保留文本与新 ID，并将 feedback_state 降级。"""
        if token_budget is not None:
            bundle = pack_retrieval_bundle(bundle, token_budget)
        elif bundle.used_tokens_estimate is None or bundle.truncated is None:
            bundle = RetrievalBundle(
                query_id=bundle.query_id,
                answerability=bundle.answerability,
                items=bundle.items,
                used_tokens_estimate=sum(estimate_tokens(item.text) for item in bundle.items),
                truncated=False,
            )
        self.last_error = None
        feedback_ids = [self._new_feedback_id() for _ in bundle.items]
        created_at = self.clock()
        exposures = [
            (
                feedback_id,
                bundle.query_id,
                item.type,
                item.id,
                rank,
                item.score,
                created_at,
            )
            for rank, (item, feedback_id) in enumerate(zip(bundle.items, feedback_ids), 1)
        ]
        feedback_state: FeedbackState = "available"
        if exposures:
            try:
                inserted = self.persist_exposures(exposures)
                if inserted != len(exposures):
                    raise RuntimeError(f"exposure batch incomplete: expected {len(exposures)}, inserted {inserted}")
            except Exception as error:
                self.last_error = error
                feedback_state = "degraded"
                LOGGER.warning(
                    "context packet exposure persistence failed: %s",
                    type(error).__name__,
                )
        return {
            "schema_major": 1,
            "schema_minor": 0,
            "query_id": bundle.query_id,
            "answerability": bundle.answerability,
            "feedback_state": feedback_state,
            "items": [
                {
                    "type": item.type,
                    "id": item.id,
                    "text": item.text,
                    "evidence": [dict(reference) for reference in item.evidence],
                    "feedback_id": feedback_id,
                }
                for item, feedback_id in zip(bundle.items, feedback_ids)
            ],
            "used_tokens_estimate": int(bundle.used_tokens_estimate or 0),
            "truncated": bool(bundle.truncated),
        }

    materialize = assemble

    def _new_feedback_id(self) -> str:
        feedback_id = str(self.feedback_id_factory()).strip()
        if not feedback_id:
            raise ValueError("feedback_id_factory returned an empty identifier")
        return feedback_id
