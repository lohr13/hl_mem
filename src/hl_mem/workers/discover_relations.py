"""有界关系候选发现与审计/自动应用 worker。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from hl_mem.core.vector import cosine_similarity
from hl_mem.domain.claims.conflicts import compute_claim_pair_key
from hl_mem.domain.relations import RelationType
from hl_mem.lifecycle import assert_transition
from hl_mem.llm.client import LLMClient
from hl_mem.llm.types import (
    LLMMessage,
    LLMRequest,
    StructuredOutputMode,
    StructuredOutputSpec,
)
from hl_mem.protocols import ClaimRow, RelationDiscoveryProtocol, RelationProposal
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.relation_proposals import RelationProposalRepository

ALLOWED_RELATIONS = frozenset(item.value for item in RelationType)
AUTO_RELATIONS = frozenset({"about", "follows", "supports"})

RELATION_DISCOVERY_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "relations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                    "relation": {"type": "string", "enum": sorted(ALLOWED_RELATIONS)},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "rationale": {"type": "string"},
                    "supporting_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "from",
                    "to",
                    "relation",
                    "confidence",
                    "rationale",
                    "supporting_ids",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["relations"],
    "additionalProperties": False,
}

RELATION_DISCOVERY_SYSTEM_PROMPT = (
    "你是 Claim 关系审计器。只能从给定 source 和 candidates 中提案，不得创造 ID。"
    "关系仅限 about/follows/supports/contradicts/summarizes。"
    "必须返回且只返回这个字段契约的 JSON 对象："
    '{"relations":[{"from":"已有ID","to":"已有ID",'
    '"relation":"about","confidence":0.95,"rationale":"判定依据",'
    '"supporting_ids":[]}]}'
    "。顶层字段只能是 relations；每个关系对象只能包含 from、to、relation、"
    "confidence、rationale、supporting_ids。from/to 必须使用输入中的完整 ID，"
    "confidence 必须是 0 到 1 的数字，supporting_ids 只能引用输入 ID。"
    '没有充分关系时返回 {"relations":[]}。禁止使用 proposals 或 target_id 字段。'
)


def _compact_claim(claim: dict[str, Any]) -> dict[str, Any]:
    """仅保留关系判定所需字段，避免向模型发送完整记录。"""
    return {
        key: claim.get(key)
        for key in (
            "id",
            "subject_entity_id",
            "predicate",
            "value",
            "canonical_slot",
            "topic_tags",
            "entities",
        )
    }


def _claim_row(claim: dict[str, Any]) -> ClaimRow:
    """在动态 SQLite 行与关系发现协议之间建立显式类型边界。"""
    row = ClaimRow(
        id=str(claim["id"]),
        namespace_key=str(claim["namespace_key"]),
        subject_entity_id=str(claim.get("subject_entity_id") or ""),
        predicate=str(claim.get("predicate") or ""),
        value=claim.get("value"),
        status=str(claim["status"]),
        confidence=float(claim.get("confidence") or 0.0),
        canonical_attribute=claim.get("canonical_attribute"),
        canonical_slot=claim.get("canonical_slot"),
        topic_tags=list(claim.get("topic_tags") or []),
        valid_from=claim.get("valid_from"),
        valid_to=claim.get("valid_to"),
        recorded_from=claim.get("recorded_from"),
        recorded_to=claim.get("recorded_to"),
        access_count=int(claim.get("access_count") or 0),
        helpful_rate=float(claim.get("helpful_rate") or 0.0),
    )
    embedding = claim.get("embedding_dense")
    if isinstance(embedding, bytes):
        row["embedding_dense"] = embedding
    return row


class LLMRelationDiscoverer(RelationDiscoveryProtocol):
    """复用统一 LLM 客户端的一次性批量关系判定器。"""

    def __init__(self, client: LLMClient) -> None:
        self.client = client

    def propose(
        self,
        source_claim: ClaimRow,
        candidates: list[ClaimRow],
        *,
        max_proposals: int,
    ) -> list[RelationProposal]:
        """生成最多 max_proposals 条结构化关系提案。"""
        payload = {
            "source": _compact_claim(dict(source_claim)),
            "candidates": [_compact_claim(dict(candidate)) for candidate in candidates],
            "max_proposals": max_proposals,
        }
        response = self.client.complete(
            LLMRequest(
                messages=[
                    LLMMessage(
                        role="system",
                        content=RELATION_DISCOVERY_SYSTEM_PROMPT,
                    ),
                    LLMMessage(
                        role="user",
                        content=json.dumps(payload, ensure_ascii=False, default=str),
                    ),
                ],
                structured_output=StructuredOutputSpec(
                    name="relation_proposals",
                    schema=RELATION_DISCOVERY_OUTPUT_SCHEMA,
                    preferred_mode=StructuredOutputMode.JSON_SCHEMA,
                ),
            )
        )
        decoded = response.content if isinstance(response.content, dict) else json.loads(response.content)
        result: list[RelationProposal] = []
        for item in decoded.get("relations", [])[:max_proposals]:
            try:
                result.append(
                    RelationProposal(
                        from_claim_id=str(item["from"]),
                        to_claim_id=str(item["to"]),
                        relation=str(item["relation"]),
                        confidence=float(item["confidence"]),
                        rationale=str(item["rationale"]),
                        supporting_claim_ids=tuple(str(value) for value in item.get("supporting_ids", [])),
                        model=self.client.model,
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return result


def build_neighbor_pool(
    connection: sqlite3.Connection,
    source_claim: dict[str, Any],
    pool_limit: int,
) -> list[ClaimRow]:
    """用单条参数化 SQL 构造同 namespace 的有界候选池。"""
    if pool_limit < 1:
        raise ValueError("pool_limit must be positive")
    scan_limit = pool_limit * 4
    rows = connection.execute(
        """
        SELECT c.*,
               CASE WHEN ? IS NOT NULL AND c.canonical_slot=? THEN 1 ELSE 0 END AS same_slot,
               CASE WHEN EXISTS (
                   SELECT 1 FROM json_each(COALESCE(c.topic_tags_json,'[]')) candidate_tag
                   JOIN json_each(?) source_tag ON source_tag.value=candidate_tag.value
               ) THEN 1 ELSE 0 END AS shared_tag,
               CASE WHEN EXISTS (
                   SELECT 1 FROM json_each(COALESCE(c.entities_json,'[]')) candidate_entity
                   JOIN json_each(?) source_entity ON source_entity.value=candidate_entity.value
               ) THEN 1 ELSE 0 END AS shared_entity,
               CASE WHEN c.subject_entity_id IS ? THEN 1 ELSE 0 END AS same_subject
        FROM claims AS c
        WHERE c.namespace_key=? AND c.status IN ('active','disputed') AND c.id<>?
        ORDER BY same_slot DESC,shared_tag DESC,shared_entity DESC,same_subject DESC,
                 c.recorded_from DESC,c.id ASC
        LIMIT ?
        """,
        (
            source_claim.get("canonical_slot"),
            source_claim.get("canonical_slot"),
            json.dumps(source_claim.get("topic_tags") or []),
            json.dumps(source_claim.get("entities") or []),
            source_claim.get("subject_entity_id"),
            source_claim["namespace_key"],
            source_claim["id"],
            scan_limit,
        ),
    ).fetchall()
    candidates: list[dict[str, Any]] = []
    for stored_row in rows:
        candidate = ClaimRepository._decode_claim(dict(stored_row))
        if candidate is None:
            raise RuntimeError("SQLite returned an empty claim row")
        candidates.append(candidate)
    source_embedding = source_claim.get("embedding_dense")
    for candidate in candidates:
        candidate["_dense_similarity"] = (
            cosine_similarity(source_embedding, candidate["embedding_dense"])
            if source_embedding and candidate.get("embedding_dense")
            else -1.0
        )
    candidates.sort(
        key=lambda item: (
            -int(item.get("same_slot", 0)),
            -int(item.get("shared_tag", 0)),
            -int(item.get("shared_entity", 0)),
            -int(item.get("same_subject", 0)),
            -float(item["_dense_similarity"]),
            str(item["id"]),
        )
    )
    return [_claim_row(item) for item in candidates[:pool_limit]]


def _validate_proposal(
    proposal: RelationProposal,
    claims: dict[str, dict[str, Any]],
) -> str | None:
    if proposal.relation not in ALLOWED_RELATIONS:
        return "invalid_relation"
    if proposal.from_claim_id == proposal.to_claim_id:
        return "self_loop"
    if not 0.0 <= proposal.confidence <= 1.0:
        return "confidence_out_of_range"
    from_claim = claims.get(proposal.from_claim_id)
    to_claim = claims.get(proposal.to_claim_id)
    if from_claim is None or to_claim is None:
        return "missing_endpoint"
    endpoints = (from_claim, to_claim)
    if from_claim["namespace_key"] != to_claim["namespace_key"]:
        return "cross_namespace"
    if any(endpoint["status"] not in {"active", "disputed"} for endpoint in endpoints):
        return "inactive_endpoint"
    if any(support_id not in claims for support_id in proposal.supporting_claim_ids):
        return "missing_support"
    if any(claims[support_id]["status"] not in {"active", "disputed"} for support_id in proposal.supporting_claim_ids):
        return "inactive_support"
    return None


def _find_or_insert_relation(
    connection: sqlite3.Connection,
    proposal_id: str,
    proposal: RelationProposal,
    now: str,
) -> str:
    row = connection.execute(
        "SELECT id FROM memory_relations WHERE from_id=? AND to_id=? AND relation=? "
        "ORDER BY confidence DESC,created_at,id LIMIT 1",
        (proposal.from_claim_id, proposal.to_claim_id, proposal.relation),
    ).fetchone()
    if row:
        return str(row["id"])
    relation_id = uuid.uuid4().hex
    evidence = {
        "proposal_id": proposal_id,
        "model": proposal.model,
        "rationale": proposal.rationale,
        "supporting_claim_ids": proposal.supporting_claim_ids,
    }
    connection.execute(
        "INSERT INTO memory_relations(id,from_id,to_id,relation,confidence,evidence_json,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            relation_id,
            proposal.from_claim_id,
            proposal.to_claim_id,
            proposal.relation,
            proposal.confidence,
            json.dumps(evidence, ensure_ascii=False),
            now,
        ),
    )
    return relation_id


def discover_relations(
    connection: sqlite3.Connection,
    discoverer: RelationDiscoveryProtocol,
    claim_id: str,
    *,
    mode: str,
    pool_limit: int,
    max_proposals: int,
    auto_apply_confidence: float,
    conflict_confidence: float,
) -> dict[str, int]:
    """发现、审计并按模式原子应用一批关系提案。"""
    if mode == "off":
        return {
            "candidates": 0,
            "proposals": 0,
            "applied": 0,
            "conflicts": 0,
            "rejected": 0,
        }
    source = ClaimRepository(connection).get_claim(claim_id)
    if source is None or source["status"] not in {"active", "disputed"}:
        return {
            "candidates": 0,
            "proposals": 0,
            "applied": 0,
            "conflicts": 0,
            "rejected": 0,
        }
    candidates = build_neighbor_pool(connection, source, pool_limit)
    proposed = discoverer.propose(_claim_row(source), candidates, max_proposals=max_proposals)
    ids = list(
        dict.fromkeys(
            [source["id"], *(item["id"] for item in candidates)]
            + [support for item in proposed for support in item.supporting_claim_ids]
            + [endpoint for item in proposed for endpoint in (item.from_claim_id, item.to_claim_id)]
        )
    )
    repository = RelationProposalRepository(connection)
    counts = {
        "candidates": len(candidates),
        "proposals": 0,
        "applied": 0,
        "conflicts": 0,
        "rejected": 0,
    }
    now = datetime.now(timezone.utc).isoformat()
    run_id = uuid.uuid4().hex
    connection.execute("BEGIN IMMEDIATE")
    try:
        # LLM 调用期间端点或证据可能已被其他 worker 关闭；写事务拿锁后必须重读。
        claims = ClaimRepository(connection).batch_get_claims(ids)
        for proposal in proposed[:max_proposals]:
            if proposal.relation not in ALLOWED_RELATIONS:
                counts["rejected"] += 1
                continue
            reason = _validate_proposal(proposal, claims)
            proposal_id = repository.insert_proposal(
                {
                    "source_claim_id": proposal.from_claim_id,
                    "run_id": run_id,
                    "target_claim_id": proposal.to_claim_id,
                    "relation": proposal.relation,
                    "confidence": proposal.confidence,
                    "rationale": proposal.rationale,
                    "supporting_claim_ids": proposal.supporting_claim_ids,
                    "model": proposal.model,
                    "mode": mode,
                    "status": "pending",
                    "created_at": now,
                },
                commit=False,
            )
            if proposal_id is None:
                continue
            counts["proposals"] += 1
            if mode == "audit":
                continue
            status, relation_id, conflict_case_id = "rejected", None, None
            if reason is not None:
                decision_reason = (
                    "stale-input"
                    if reason
                    in {
                        "missing_endpoint",
                        "inactive_endpoint",
                        "missing_support",
                        "inactive_support",
                    }
                    else reason
                )
            elif proposal.relation == "summarizes":
                decision_reason = "topic_summary_builder_only"
            elif proposal.relation in AUTO_RELATIONS and proposal.confidence >= auto_apply_confidence:
                relation_id = _find_or_insert_relation(connection, proposal_id, proposal, now)
                status, decision_reason = "applied", "auto_confidence_threshold"
                counts["applied"] += 1
            elif proposal.relation == "contradicts" and proposal.confidence >= conflict_confidence:
                pair_key = compute_claim_pair_key(proposal.from_claim_id, proposal.to_claim_id)
                row = connection.execute("SELECT id FROM conflict_cases WHERE pair_key=?", (pair_key,)).fetchone()
                conflict_case_id = str(row["id"]) if row else uuid.uuid4().hex
                if row is None:
                    connection.execute(
                        "INSERT INTO conflict_cases(id,pair_key,left_claim_id,right_claim_id,status,decision,"
                        "rationale,confidence,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            conflict_case_id,
                            pair_key,
                            proposal.from_claim_id,
                            proposal.to_claim_id,
                            "manual_required",
                            "contradicts",
                            proposal.rationale,
                            proposal.confidence,
                            now,
                        ),
                    )
                endpoint_update_failed = False
                for endpoint_id in (proposal.from_claim_id, proposal.to_claim_id):
                    endpoint = claims[endpoint_id]
                    if endpoint["status"] == "active":
                        assert_transition("active", "disputed")
                        cursor = connection.execute(
                            "UPDATE claims SET status='disputed' WHERE id=? AND status='active'",
                            (endpoint_id,),
                        )
                        if cursor.rowcount != 1:
                            endpoint_update_failed = True
                            break
                if endpoint_update_failed:
                    status, decision_reason = "rejected", "stale-input"
                else:
                    status, decision_reason = (
                        "conflict_created",
                        "contradiction_threshold",
                    )
                    counts["conflicts"] += 1
            else:
                decision_reason = "below_confidence_threshold"
            if status == "rejected":
                counts["rejected"] += 1
            repository.update_proposal_status(
                proposal_id,
                status,
                decision_reason=decision_reason,
                relation_id=relation_id,
                conflict_case_id=conflict_case_id,
                decided_at=now,
                commit=False,
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return counts
