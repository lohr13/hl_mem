"""Context Packet v1 的无状态组装与 exposure 物化。"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, cast

from hl_mem.application.answerability import Answerability
from hl_mem.experience.service import ExperienceService

LOGGER = logging.getLogger(__name__)

FeedbackState = Literal["available", "degraded"]
MemoryType = Literal["claim", "observation", "policy", "episode", "trace"]

_ANSWERABILITY_VALUES = frozenset({"supported", "low_confidence", "no_evidence"})
_MEMORY_TYPE_VALUES = frozenset({"claim", "observation", "policy", "episode", "trace"})
_GENERIC_RELATION_ACTIONS = frozenset(
    {
        "attribute",
        "config",
        "fact",
        "facts",
        "identity",
        "preference",
        "state",
        "unknown",
        "value",
        "事实",
        "偏好",
        "值",
        "属性",
        "状态",
        "身份",
        "配置",
    }
)
RETRIEVAL_BUNDLE_SCHEMA_MAJOR = 1
RETRIEVAL_BUNDLE_SCHEMA_MINOR = 1
CONTEXT_PACKET_CLAIM_LIMIT = 10


class UnknownSchemaMajorError(ValueError):
    """Wire payload 使用了当前进程无法安全解释的 schema major。"""


def normalize_relation_components(
    role: object,
    action: object,
    object_: object,
) -> tuple[str, str, str] | None:
    """把完整 RAO 三元组归一为单行；任一缺失时不产生关系表示。"""

    values = tuple(" ".join(str(value or "").split()) for value in (role, action, object_))
    if not all(values):
        return None
    return cast(tuple[str, str, str], values)


def project_claim_relation(claim: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """Project explicit RAO, or a semantic predicate whose object is already public."""

    raw_qualifiers = claim.get("qualifiers")
    qualifiers = raw_qualifiers if isinstance(raw_qualifiers, Mapping) else {}
    explicit = normalize_relation_components(
        qualifiers.get("role"),
        qualifiers.get("action"),
        qualifiers.get("object"),
    )
    if explicit is not None:
        return explicit
    action = " ".join(str(claim.get("predicate") or "").split())
    if not action or action.casefold() in _GENERIC_RELATION_ACTIONS:
        return None
    value = claim.get("value")
    serialized_value = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) if value is not None else ""
    )
    public_text = claim.get("index_text")
    if not isinstance(public_text, str) or serialized_value not in public_text:
        return None
    return normalize_relation_components(
        claim.get("subject_entity_id"),
        action,
        serialized_value,
    )


def render_memory_text(
    text: str,
    *,
    role: object = None,
    action: object = None,
    object_: object = None,
) -> str:
    """渲染 reader 可见文本；只有完整 RAO 才追加结构化关系行。"""

    relation = normalize_relation_components(role, action, object_)
    if relation is None:
        return text
    normalized_role, normalized_action, normalized_object = relation
    return f"{text}\nrelation: {normalized_role} → {normalized_action} → {normalized_object}"


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
    role: str | None = None
    action: str | None = None
    object: str | None = None

    def __post_init__(self) -> None:
        if self.type not in _MEMORY_TYPE_VALUES:
            raise ValueError(f"unsupported memory type: {self.type}")
        if not self.id:
            raise ValueError("retrieval bundle item id must be non-empty")
        if not isinstance(self.text, str):
            raise TypeError("retrieval bundle item text must be a string")
        relation_values = (self.role, self.action, self.object)
        if any(value is not None for value in relation_values):
            if self.type != "claim":
                raise ValueError("only claim retrieval bundle items may carry relation fields")
            if any(not isinstance(value, str) or not value.strip() for value in relation_values):
                raise ValueError("retrieval bundle relation fields must be complete non-empty strings")


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
        if self.answerability == "no_evidence" and self.items:
            raise ValueError("no_evidence retrieval bundle must not contain items")
        if self.used_tokens_estimate is not None and self.used_tokens_estimate < 0:
            raise ValueError("used_tokens_estimate must be non-negative")


def retrieval_bundle_to_dict(bundle: RetrievalBundle) -> dict[str, Any]:
    """序列化 receipt-free bundle；wire payload 永不携带 feedback_id。"""
    return {
        "schema_major": RETRIEVAL_BUNDLE_SCHEMA_MAJOR,
        "schema_minor": RETRIEVAL_BUNDLE_SCHEMA_MINOR,
        "query_id": bundle.query_id,
        "answerability": bundle.answerability,
        "items": [
            {
                "type": item.type,
                "id": item.id,
                "text": item.text,
                "evidence": [dict(reference) for reference in item.evidence],
                "score": item.score,
                **({"role": item.role, "action": item.action, "object": item.object} if item.role is not None else {}),
            }
            for item in bundle.items
        ],
        "used_tokens_estimate": bundle.used_tokens_estimate,
        "truncated": bundle.truncated,
    }


def retrieval_bundle_from_dict(payload: Mapping[str, Any]) -> RetrievalBundle:
    """校验并反序列化 Hermes 内部 receipt-free bundle wire payload。"""
    schema_major = payload.get("schema_major")
    if (
        not isinstance(schema_major, int)
        or isinstance(schema_major, bool)
        or schema_major != RETRIEVAL_BUNDLE_SCHEMA_MAJOR
    ):
        raise UnknownSchemaMajorError(f"unsupported retrieval bundle schema major: {schema_major!r}")
    query_id = payload.get("query_id")
    answerability = payload.get("answerability")
    raw_items = payload.get("items")
    if not isinstance(query_id, str) or not query_id:
        raise ValueError("retrieval bundle query_id must be a non-empty string")
    if answerability not in _ANSWERABILITY_VALUES:
        raise ValueError(f"unsupported answerability: {answerability!r}")
    if not isinstance(raw_items, list):
        raise TypeError("retrieval bundle items must be a list")

    items: list[RetrievalBundleItem] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            raise TypeError("retrieval bundle item must be an object")
        raw_type = raw_item.get("type")
        raw_id = raw_item.get("id")
        raw_text = raw_item.get("text")
        if not isinstance(raw_type, str):
            raise TypeError("retrieval bundle item type must be a string")
        if not isinstance(raw_id, str) or not raw_id:
            raise ValueError("retrieval bundle item id must be a non-empty string")
        if not isinstance(raw_text, str):
            raise TypeError("retrieval bundle item text must be a string")
        raw_evidence = raw_item.get("evidence", [])
        if not isinstance(raw_evidence, list):
            raise TypeError("retrieval bundle item evidence must be a list")
        evidence = tuple(dict(reference) for reference in raw_evidence if isinstance(reference, Mapping))
        raw_score = raw_item.get("score")
        score = float(raw_score) if raw_score is not None else None
        relation_values = tuple(raw_item.get(key) for key in ("role", "action", "object"))
        if any(value is not None for value in relation_values) and not all(
            isinstance(value, str) and value.strip() for value in relation_values
        ):
            raise ValueError("retrieval bundle relation fields must be complete non-empty strings")
        role, action, object_ = cast(tuple[str | None, str | None, str | None], relation_values)
        items.append(
            RetrievalBundleItem(
                cast(MemoryType, raw_type),
                raw_id,
                raw_text,
                evidence,
                score,
                role,
                action,
                object_,
            )
        )

    used_tokens_estimate = payload.get("used_tokens_estimate")
    if used_tokens_estimate is not None and (
        not isinstance(used_tokens_estimate, int) or isinstance(used_tokens_estimate, bool)
    ):
        raise TypeError("used_tokens_estimate must be an integer or null")
    truncated = payload.get("truncated")
    if truncated is not None and not isinstance(truncated, bool):
        raise TypeError("truncated must be a boolean or null")
    return RetrievalBundle(
        query_id=query_id,
        answerability=cast(Answerability, answerability),
        items=tuple(items),
        used_tokens_estimate=used_tokens_estimate,
        truncated=truncated,
    )


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
    claim_count = 0
    for item in candidates:
        if item.type == "claim" and claim_count >= CONTEXT_PACKET_CLAIM_LIMIT:
            continue
        cost = estimate_tokens(
            render_memory_text(
                item.text,
                role=item.role,
                action=item.action,
                object_=item.object,
            )
        )
        if used + cost > token_budget:
            continue
        packed.append(item)
        used += cost
        if item.type == "claim":
            claim_count += 1
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
        truncated=bool(bundle.truncated) or truncated,
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
        else:
            unbounded_cost = sum(
                estimate_tokens(
                    render_memory_text(
                        item.text,
                        role=item.role,
                        action=item.action,
                        object_=item.object,
                    )
                )
                for item in bundle.items
            )
            packed, used, truncated = pack_retrieval_items(bundle.items, unbounded_cost)
            bundle = RetrievalBundle(
                query_id=bundle.query_id,
                answerability=bundle.answerability,
                items=packed,
                used_tokens_estimate=used,
                truncated=bool(bundle.truncated) or truncated,
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
            "schema_minor": RETRIEVAL_BUNDLE_SCHEMA_MINOR,
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
                    **(
                        {"role": item.role, "action": item.action, "object": item.object}
                        if item.role is not None
                        else {}
                    ),
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
