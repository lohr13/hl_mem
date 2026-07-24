"""记忆召回应用服务。执行 FTS + 向量 + reranker 混合召回，管理访问记录和反馈。"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Any

from hl_mem.application.ingest import new_id
from hl_mem.config import RECALL_DEFAULT_LIMIT, RECALL_VECTOR_SCAN_LIMIT
from hl_mem.experience.service import ExperienceService
from hl_mem.observability.audit import current_audit
from hl_mem.protocols import EmbedderProtocol, RerankerProtocol
from hl_mem.domain.recall import RecallIntent, route_recall_intent
from hl_mem.recall.recall_pipeline import hybrid_claims, matching_policies
from hl_mem.recall.relation_expansion import RelationExpansionConfig
from hl_mem.recall.trace import SearchPhaseMetrics, SearchTrace, SearchTracer
from hl_mem.storage.repository import ClaimRepository, EvidenceRepository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RecallService:
    """记忆召回应用服务。"""

    def __init__(
        self,
        connection: Any,
        embedder: EmbedderProtocol | Any,
        reranker: RerankerProtocol | None = None,
        relation_config: RelationExpansionConfig | None = None,
    ) -> None:
        self.connection = connection
        self.embedder = embedder
        self.reranker = reranker
        self.relation_config = relation_config or RelationExpansionConfig()

    def recall(
        self,
        query: str,
        limit: int = RECALL_DEFAULT_LIMIT,
        as_of: str | None = None,
        intent: RecallIntent | str | None = None,
        known_as_of: str | None = None,
        query_id: str | None = None,
        token_budget: int | None = None,
        context_mode: str | None = None,
        namespace: str = "default",
        debug: bool = False,
    ) -> dict[str, Any]:
        """执行混合召回并返回 claim、策略、证据及查询标识。"""
        total_started = time.perf_counter_ns()
        query_id = query_id or new_id()
        selected_intent = RecallIntent(intent or route_recall_intent(query, as_of))
        tracer = (
            SearchTracer(
                SearchTrace(
                    query_id=query_id,
                    query_hash=hashlib.sha256(query.encode()).hexdigest(),
                    intent=selected_intent.value,
                    limit=limit,
                    candidate_limit=min(RECALL_VECTOR_SCAN_LIMIT, max(limit * 5, 50)),
                    candidates={},
                    phases=SearchPhaseMetrics(),
                )
            )
            if debug
            else None
        )
        claims = hybrid_claims(
            ClaimRepository(self.connection),
            query,
            self.embedder.embed_one(query),
            limit,
            as_of,
            self.reranker,
            intent=selected_intent,
            known_as_of=known_as_of,
            namespace=namespace,
            relation_connection=self.connection,
            relation_config=self.relation_config,
            tracer=tracer,
        )
        self._record_access(claims)
        self._record_feedback(claims, query_id)
        assembly_started = time.perf_counter_ns()
        results = self._assemble_results(claims, namespace)
        if tracer is not None:
            tracer.trace.phases.assembly_us = (time.perf_counter_ns() - assembly_started) // 1000
        observations = self._assemble_observations([claim["id"] for claim in claims])
        policies = matching_policies(
            ExperienceService(self.connection).list_policies("active", namespace=namespace),
            query,
        )
        response = {
            "results": results,
            "observations": observations,
            "policies": policies,
            "total": len(results),
            "query_id": query_id,
        }
        if context_mode == "packed":
            response["context"] = self._assemble_context(results, observations, policies, token_budget or 2000)
        if tracer is not None:
            tracer.trace.phases.total_us = (time.perf_counter_ns() - total_started) // 1000
            response["search_trace"] = tracer.to_dict()
        return response

    def _assemble_observations(self, claim_ids: list[str]) -> list[dict[str, Any]]:
        """查询与召回 Claim 相关的活跃派生记忆。"""
        if not claim_ids:
            return []
        placeholders = ",".join("?" for _ in claim_ids)
        rows = self.connection.execute(
            "SELECT d.id,d.kind,d.body,d.confidence,d.updated_at FROM derivations d "
            "JOIN evidence_links e ON e.derived_id=d.id AND e.derived_type=d.kind "
            f"WHERE d.status='active' AND e.evidence_type='claim' AND e.evidence_id IN ({placeholders}) "
            "GROUP BY d.id ORDER BY d.updated_at DESC LIMIT 10",
            claim_ids,
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _assemble_context(
        claims: list[dict[str, Any]],
        observations: list[dict[str, Any]],
        policies: list[dict[str, Any]],
        token_budget: int,
    ) -> dict[str, Any]:
        """按优先级跨类型组装受 token 预算约束的上下文。"""
        all_items = (
            [{"type": "claim", "data": item, "priority": 2} for item in claims]
            + [{"type": "observation", "data": item, "priority": 1} for item in observations]
            + [{"type": "policy", "data": item, "priority": 0} for item in policies]
        )
        all_items.sort(key=lambda item: -item["priority"])
        packed: list[dict[str, Any]] = []
        used = 0
        truncated = False
        for item in all_items:
            data = item["data"]
            text = str(data.get("text") or data.get("body") or data.get("procedure") or "")
            cost = max(1, (len(text) + 1) // 2)
            if used + cost > token_budget:
                truncated = True
                continue
            packed.append(item)
            used += cost
            if used >= token_budget:
                truncated = len(packed) < len(all_items)
                break
        return {"context_items": packed, "used_tokens_estimate": used, "truncated": truncated}

    def _record_access(self, claims: list[dict[str, Any]]) -> None:
        try:
            ClaimRepository(self.connection).record_access([claim["id"] for claim in claims], _now())
        except Exception as error:
            self._emit_failure("access_record", "access_record_failed", error, len(claims))

    def _record_feedback(self, claims: list[dict[str, Any]], query_id: str) -> None:
        try:
            recorded_at = _now()
            ExperienceService(self.connection).record_feedback_batch(
                [
                    (
                        new_id(), query_id, "claim", claim["id"], rank,
                        float(claim.get("_score", 0.0)), 0, None, None, recorded_at,
                    )
                    for rank, claim in enumerate(claims, 1)
                ]
            )
        except Exception as error:
            self._emit_failure("feedback_record", "feedback_record_failed", error, len(claims))

    @staticmethod
    def _emit_failure(operation: str, outcome: str, error: Exception, claim_count: int) -> None:
        try:
            current_audit().emit(
                "recall",
                operation,
                outcome,
                detail={"error_class": type(error).__name__, "claim_count": claim_count},
            )
        except Exception:
            pass

    def _assemble_results(
        self,
        claims: list[dict[str, Any]],
        namespace: str = "default",
    ) -> list[dict[str, Any]]:
        if not claims:
            return []
        evidence_repo = EvidenceRepository(self.connection)
        claim_repo = ClaimRepository(self.connection)
        claim_ids = [claim["id"] for claim in claims]
        all_evidence = self._batch_evidence(evidence_repo, claim_ids)
        superseded_ids = [
            claim["superseded_by_id"]
            for claim in claims
            if claim.get("superseded_by_id")
        ]
        replacement_map = self._batch_replacements(claim_repo, superseded_ids)
        relations_map = self._batch_relations(claim_ids)
        rivals_map = self._batch_rivals(claims, namespace)
        results: list[dict[str, Any]] = []
        for claim in claims:
            evidence = all_evidence.get(claim["id"], [])
            decoded = json.loads(claim["value_json"])
            text = (
                decoded.get("old_value")
                if isinstance(decoded, dict) and decoded.get("_type") == "superseded_value"
                else decoded
            )
            replacement = replacement_map.get(claim.get("superseded_by_id"))
            result = {
                "type": "claim",
                "id": claim["id"],
                "text": text,
                "status": claim["status"],
                "confidence": claim["confidence"],
                "valid_from": claim["valid_from"],
                "replacement": replacement,
                "evidence": evidence,
                "relations": relations_map.get(claim["id"], []),
            }
            if claim["status"] == "disputed" and claim.get("conflict_key"):
                result["conflicts"] = rivals_map.get(claim["id"], [])
            results.append(result)
        return results

    @staticmethod
    def _batch_evidence(
        evidence_repo: EvidenceRepository,
        claim_ids: list[str],
    ) -> dict[str, list[dict[str, str]]]:
        """批量加载 claim 的证据链接。"""
        return evidence_repo.batch_get_links_for_derived("claim", claim_ids)

    @staticmethod
    def _batch_replacements(
        claim_repo: ClaimRepository,
        superseded_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        """批量加载被替代 claim 的替代项。"""
        claims = claim_repo.batch_get_claims(superseded_ids)
        return {
            claim_id: {
                "id": claim["id"],
                "text": json.loads(claim["value_json"]),
                "valid_from": claim["valid_from"],
            }
            for claim_id, claim in claims.items()
        }

    def _batch_relations(self, claim_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        """批量加载 claim 的关系。"""
        from hl_mem.domain.relations import get_relations_batch

        return get_relations_batch(self.connection, claim_ids)

    def _batch_rivals(
        self,
        claims: list[dict[str, Any]],
        namespace: str,
    ) -> dict[str, list[dict[str, Any]]]:
        """批量加载 disputed claim 的同 namespace 冲突项并精确映射。"""
        disputed_claims = [
            claim
            for claim in claims
            if claim["status"] == "disputed" and claim.get("conflict_key")
        ]
        if not disputed_claims:
            return {}
        unique_keys = list(dict.fromkeys(claim["conflict_key"] for claim in disputed_claims))
        rivals_by_key: dict[str, list[dict[str, Any]]] = {key: [] for key in unique_keys}
        for start in range(0, len(unique_keys), 500):
            chunk = unique_keys[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = self.connection.execute(
                "SELECT id,value_json,conflict_key FROM claims "
                f"WHERE conflict_key IN ({placeholders}) "
                "AND status='disputed' AND namespace_key=?",
                (*chunk, namespace),
            ).fetchall()
            for row in rows:
                rival = dict(row)
                rivals_by_key[rival["conflict_key"]].append(
                    {"id": rival["id"], "value_json": rival["value_json"]}
                )
        return {
            claim["id"]: [
                rival
                for rival in rivals_by_key[claim["conflict_key"]]
                if rival["id"] != claim["id"]
            ]
            for claim in disputed_claims
        }
