"""从私有小语料构建隔离数据库并评测中文生产召回。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any, Protocol

from hl_mem.application.ingest import claim_text, compute_fact_hash
from hl_mem.domain.claims.attributes import (
    normalize_topic_tags,
    validate_canonical_attribute,
    validate_slot_instance,
)
from hl_mem.domain.claims.claim import build_index_text
from hl_mem.domain.recall import RecallIntent
from hl_mem.protocols import EmbedderProtocol, embed_queries
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository

EVAL_TIME = "2026-08-13T00:00:00+00:00"
_EXPECTED_TYPES = frozenset({"claim", "empty"})
_INTENTS = frozenset(intent.value for intent in RecallIntent)
_INTENT_SOURCES = frozenset({"explicit", "keyword", "fallback", "llm"})
_ABSTENTIONS = frozenset({"no_evidence", "low_confidence"})


class DatasetError(ValueError):
    """私有评测数据不满足隔离评测契约。"""


@dataclass(frozen=True)
class CorpusClaim:
    """可重建评测库中的一条规范化 claim。"""

    memory_id: str
    namespace: str
    subject: str
    predicate: str
    value: str
    canonical_attribute: str
    canonical_slot: str | None
    qualifiers: dict[str, Any]
    topic_tags: tuple[str, ...]
    importance: float


@dataclass(frozen=True)
class ChineseRecallCase:
    """绑定稳定 memory ID 的中文召回样例。"""

    case_id: str
    namespace: str
    query: str
    expected_type: str
    expected_memory_ids: tuple[str, ...]
    expected_intent: str
    expected_intent_source: str
    slice: str
    intent_override: str | None = None


@dataclass(frozen=True)
class CaseResult:
    """单条 query 的可诊断评分。"""

    case_id: str
    slice: str
    returned_ids: tuple[str, ...]
    matched_expected_ids: tuple[str, ...]
    expected_count: int
    rank: int | None
    answerability: str
    top_score: float | None
    runner_up_score: float | None
    top_reranker_score: float | None
    top_dense_score: float | None
    top_relevance_decision: str | None
    top_relevance_reason: str | None
    top_channels: tuple[str, ...]
    actual_intent: str
    intent_source: str
    intent_correct: bool
    correct_positive_answer: bool | None
    correct_no_answer: bool | None


@dataclass(frozen=True)
class EvaluationReport:
    """隔离中文评测的聚合指标与逐条结果。"""

    items: tuple[CaseResult, ...]

    @property
    def case_count(self) -> int:
        return len(self.items)

    @property
    def hit_at_1(self) -> float:
        positives = [item for item in self.items if item.correct_no_answer is None]
        return mean(item.rank == 1 for item in positives) if positives else 0.0

    @property
    def hit_at_5(self) -> float:
        positives = [item for item in self.items if item.correct_no_answer is None]
        return mean(item.rank is not None and item.rank <= 5 for item in positives) if positives else 0.0

    @property
    def mrr(self) -> float:
        positives = [item for item in self.items if item.correct_no_answer is None]
        return mean(1.0 / item.rank if item.rank is not None else 0.0 for item in positives) if positives else 0.0

    @property
    def no_answer_accuracy(self) -> float:
        no_answer = [item for item in self.items if item.correct_no_answer is not None]
        return mean(bool(item.correct_no_answer) for item in no_answer) if no_answer else 0.0

    @property
    def positive_answerability_accuracy(self) -> float:
        positives = [item for item in self.items if item.correct_positive_answer is not None]
        return mean(bool(item.correct_positive_answer) for item in positives) if positives else 0.0

    @property
    def intent_accuracy(self) -> float:
        return mean(item.intent_correct for item in self.items) if self.items else 0.0

    @property
    def mean_gold_recall(self) -> float:
        positives = [item for item in self.items if item.expected_count]
        return mean(len(item.matched_expected_ids) / item.expected_count for item in positives) if positives else 0.0

    @property
    def complete_evidence_accuracy(self) -> float:
        positives = [item for item in self.items if item.expected_count]
        return mean(len(item.matched_expected_ids) == item.expected_count for item in positives) if positives else 0.0


class RecallServiceLike(Protocol):
    """评测所需的最小 RecallService 接口。"""

    def recall(self, query: str, **kwargs: Any) -> dict[str, Any]: ...


class QueryEmbeddingCache:
    """一次批量生成 query 向量，并保持完整 EmbedderProtocol。"""

    def __init__(self, delegate: EmbedderProtocol, queries: list[str]) -> None:
        self.delegate = delegate
        self.dim = delegate.dim
        self.model = delegate.model
        unique_queries = list(dict.fromkeys(queries))
        self._queries = dict(zip(unique_queries, embed_queries(delegate, unique_queries), strict=True))

    def embed_one(self, text: str) -> bytes:
        return self.delegate.embed_one(text)

    def embed_batch(self, texts: list[str]) -> list[bytes]:
        return self.delegate.embed_batch(texts)

    def embed_query(self, text: str) -> bytes:
        try:
            return self._queries[text]
        except KeyError as error:
            raise KeyError(f"query embedding was not precomputed: {text!r}") from error

    def embed_query_batch(self, texts: list[str]) -> list[bytes]:
        return [self.embed_query(text) for text in texts]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise DatasetError(f"evaluation dataset does not exist: {path}")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise DatasetError(f"{path}:{line_number}: invalid JSON: {error.msg}") from error
        if not isinstance(row, dict):
            raise DatasetError(f"{path}:{line_number}: each line must be a JSON object")
        rows.append(row)
    if not rows:
        raise DatasetError(f"evaluation dataset is empty: {path}")
    return rows


def _required_text(row: dict[str, Any], field: str, row_id: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise DatasetError(f"{row_id}: {field} must be a non-empty string")
    return value.strip()


def _namespace(row: dict[str, Any], row_id: str) -> str:
    value = row.get("namespace", "default")
    if not isinstance(value, str) or not value.strip():
        raise DatasetError(f"{row_id}: namespace must be a non-empty string")
    return value.strip()


def _string_tuple(row: dict[str, Any], field: str, row_id: str) -> tuple[str, ...]:
    value = row.get(field)
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise DatasetError(f"{row_id}: {field} must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


def load_corpus(path: Path) -> list[CorpusClaim]:
    """读取并严格校验私有合成 corpus。"""
    claims: list[CorpusClaim] = []
    seen: set[str] = set()
    for index, row in enumerate(_read_jsonl(path), start=1):
        row_id = str(row.get("memory_id") or f"line {index}")
        memory_id = _required_text(row, "memory_id", row_id)
        if memory_id in seen:
            raise DatasetError(f"{memory_id}: duplicate memory_id")
        seen.add(memory_id)
        qualifiers = row.get("qualifiers", {})
        if not isinstance(qualifiers, dict):
            raise DatasetError(f"{memory_id}: qualifiers must be an object")
        topic_tags = _string_tuple(row, "topic_tags", memory_id)
        normalized_tags = tuple(normalize_topic_tags(topic_tags))
        if topic_tags != normalized_tags:
            raise DatasetError(f"{memory_id}: topic_tags must use unique registered tags: {normalized_tags}")
        importance = row.get("importance", 0.8)
        if not isinstance(importance, (int, float)) or isinstance(importance, bool) or not 0 <= importance <= 1:
            raise DatasetError(f"{memory_id}: importance must be between 0 and 1")
        canonical_slot = row.get("canonical_slot")
        if canonical_slot is not None and (not isinstance(canonical_slot, str) or not canonical_slot.strip()):
            raise DatasetError(f"{memory_id}: canonical_slot must be null or a non-empty string")
        predicate = _required_text(row, "predicate", memory_id)
        canonical_attribute = _required_text(row, "canonical_attribute", memory_id)
        validated_attribute = validate_canonical_attribute(predicate, canonical_attribute)
        if canonical_attribute != validated_attribute:
            raise DatasetError(
                f"{memory_id}: canonical_attribute {canonical_attribute!r} is invalid for predicate {predicate!r}"
            )
        normalized_slot = validate_slot_instance(canonical_slot, qualifiers)
        if canonical_slot != normalized_slot:
            raise DatasetError(f"{memory_id}: canonical_slot must be an operational slot with all required qualifiers")
        claims.append(
            CorpusClaim(
                memory_id=memory_id,
                namespace=_namespace(row, memory_id),
                subject=_required_text(row, "subject", memory_id),
                predicate=predicate,
                value=_required_text(row, "value", memory_id),
                canonical_attribute=canonical_attribute,
                canonical_slot=normalized_slot,
                qualifiers=dict(qualifiers),
                topic_tags=normalized_tags,
                importance=float(importance),
            )
        )
    return claims


def load_cases(path: Path, memory_ids: set[str]) -> list[ChineseRecallCase]:
    """读取 case，并确认所有 gold 都指向本次隔离 corpus。"""
    cases: list[ChineseRecallCase] = []
    seen: set[str] = set()
    for index, row in enumerate(_read_jsonl(path), start=1):
        row_id = str(row.get("case_id") or f"line {index}")
        case_id = _required_text(row, "case_id", row_id)
        if case_id in seen:
            raise DatasetError(f"{case_id}: duplicate case_id")
        seen.add(case_id)
        expected_type = _required_text(row, "expected_type", case_id)
        if expected_type not in _EXPECTED_TYPES:
            raise DatasetError(f"{case_id}: expected_type must be claim or empty")
        expected_ids = _string_tuple(row, "expected_memory_ids", case_id)
        if expected_type == "claim" and not expected_ids:
            raise DatasetError(f"{case_id}: claim case requires expected_memory_ids")
        if expected_type == "empty" and expected_ids:
            raise DatasetError(f"{case_id}: empty case cannot declare expected_memory_ids")
        missing = sorted(set(expected_ids) - memory_ids)
        if missing:
            raise DatasetError(f"{case_id}: expected_memory_ids missing from corpus: {', '.join(missing)}")
        expected_intent = _required_text(row, "expected_intent", case_id)
        if expected_intent not in _INTENTS:
            raise DatasetError(f"{case_id}: unknown expected_intent {expected_intent!r}")
        expected_source = _required_text(row, "expected_intent_source", case_id)
        if expected_source not in _INTENT_SOURCES:
            raise DatasetError(f"{case_id}: unknown expected_intent_source {expected_source!r}")
        intent_override = row.get("intent_override")
        if intent_override is not None and intent_override not in _INTENTS:
            raise DatasetError(f"{case_id}: unknown intent_override {intent_override!r}")
        if intent_override is None and expected_source == "explicit":
            raise DatasetError(f"{case_id}: explicit intent source requires intent_override")
        if intent_override is not None and expected_source != "explicit":
            raise DatasetError(f"{case_id}: intent_override requires explicit expected_intent_source")
        cases.append(
            ChineseRecallCase(
                case_id=case_id,
                namespace=_namespace(row, case_id),
                query=_required_text(row, "query", case_id),
                expected_type=expected_type,
                expected_memory_ids=expected_ids,
                expected_intent=expected_intent,
                expected_intent_source=expected_source,
                slice=_required_text(row, "slice", case_id),
                intent_override=intent_override,
            )
        )
    return cases


def build_corpus(
    connection: sqlite3.Connection,
    corpus: list[CorpusClaim],
    embedder: EmbedderProtocol,
    settings: Settings,
) -> None:
    """批量向量化并插入隔离 corpus，不创建外部事件或写开发库。"""
    prepared: list[dict[str, Any]] = []
    for claim in corpus:
        row = {
            "id": claim.memory_id,
            "namespace_key": claim.namespace,
            "subject_entity_id": claim.subject,
            "predicate": claim.predicate,
            "value": claim.value,
            "qualifiers": claim.qualifiers,
            "fact_hash": compute_fact_hash(claim.subject, claim.predicate, claim.value),
            "conflict_key": None,
            "conflict_key_version": 3,
            "legacy_conflict_key": None,
            "valid_from": EVAL_TIME,
            "recorded_from": EVAL_TIME,
            "observed_at": EVAL_TIME,
            "volatility": "stable",
            "status": "active",
            "confidence": 0.95,
            "importance": claim.importance,
            "scope": "permanent",
            "access_count": 0,
            "source_authority": "high",
            "extractor_version": "private-eval-corpus-v1",
            "embedding_model": embedder.model,
            "embedding_dim": embedder.dim,
            "canonical_attribute": claim.canonical_attribute,
            "canonical_slot": claim.canonical_slot,
            "topic_tags_json": json.dumps(claim.topic_tags, ensure_ascii=False, separators=(",", ":")),
        }
        row["index_text"] = build_index_text({**row, "topic_tags": claim.topic_tags}, mode=settings.index_text_mode)
        prepared.append(row)
    embeddings = embedder.embed_batch([claim_text(row) for row in prepared])
    if len(embeddings) != len(prepared):
        raise RuntimeError("embedder returned the wrong number of corpus embeddings")
    repository = ClaimRepository(connection, settings=settings)
    connection.execute("BEGIN IMMEDIATE")
    try:
        for row, embedding in zip(prepared, embeddings, strict=True):
            row["embedding_dense"] = embedding
            if not repository.insert_claim(row, commit=False):
                raise RuntimeError(f"duplicate corpus memory_id: {row['id']}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def evaluate_cases(
    service: RecallServiceLike,
    cases: list[ChineseRecallCase],
    *,
    limit: int,
) -> EvaluationReport:
    """调用生产召回入口，按 ID、answerability 和 trace 评分。"""
    items: list[CaseResult] = []
    for case in cases:
        response = service.recall(
            case.query,
            limit=limit,
            intent=case.intent_override,
            namespace=case.namespace,
            debug=True,
            ranking_now=EVAL_TIME,
        )
        results = response.get("results") or []
        returned_ids = tuple(
            str(result["id"]) for result in results if isinstance(result, dict) and isinstance(result.get("id"), str)
        )
        expected = set(case.expected_memory_ids)
        matched_expected_ids = tuple(memory_id for memory_id in returned_ids if memory_id in expected)
        rank = next((index for index, memory_id in enumerate(returned_ids, start=1) if memory_id in expected), None)
        raw_answerability = response.get("answerability")
        answerability = raw_answerability if isinstance(raw_answerability, str) and raw_answerability else "unknown"
        trace = response.get("search_trace") or {}
        actual_intent = str(trace.get("intent") or "") if isinstance(trace, dict) else ""
        intent_source = str(trace.get("intent_source") or "") if isinstance(trace, dict) else ""
        result_scores = [
            float(result["score"])
            for result in results
            if isinstance(result, dict) and isinstance(result.get("score"), (int, float))
        ]
        candidates = trace.get("candidates") if isinstance(trace, dict) else None
        top_trace = (
            candidates.get(returned_ids[0], {})
            if returned_ids and isinstance(candidates, dict) and isinstance(candidates.get(returned_ids[0], {}), dict)
            else {}
        )
        channel_scores = top_trace.get("channel_scores")
        channels = top_trace.get("channels")
        raw_reranker_score = top_trace.get("rerank_score")
        raw_dense_score = channel_scores.get("dense") if isinstance(channel_scores, dict) else None
        items.append(
            CaseResult(
                case_id=case.case_id,
                slice=case.slice,
                returned_ids=returned_ids,
                matched_expected_ids=matched_expected_ids,
                expected_count=len(expected),
                rank=rank,
                answerability=answerability,
                top_score=result_scores[0] if result_scores else None,
                runner_up_score=result_scores[1] if len(result_scores) > 1 else None,
                top_reranker_score=float(raw_reranker_score) if isinstance(raw_reranker_score, (int, float)) else None,
                top_dense_score=float(raw_dense_score) if isinstance(raw_dense_score, (int, float)) else None,
                top_relevance_decision=(
                    str(top_trace["relevance_decision"]) if top_trace.get("relevance_decision") is not None else None
                ),
                top_relevance_reason=(
                    str(top_trace["relevance_reason"]) if top_trace.get("relevance_reason") is not None else None
                ),
                top_channels=tuple(sorted(str(channel) for channel in channels)) if isinstance(channels, dict) else (),
                actual_intent=actual_intent,
                intent_source=intent_source,
                intent_correct=(actual_intent == case.expected_intent and intent_source == case.expected_intent_source),
                correct_positive_answer=(answerability == "supported" if case.expected_type == "claim" else None),
                correct_no_answer=(answerability in _ABSTENTIONS if case.expected_type == "empty" else None),
            )
        )
    return EvaluationReport(tuple(items))
