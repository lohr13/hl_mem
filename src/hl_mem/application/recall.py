"""记忆召回应用服务。执行 FTS + 向量 + reranker 混合召回，管理访问记录和反馈。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from hl_mem.application.ingest import new_id
from hl_mem.config import RECALL_DEFAULT_LIMIT
from hl_mem.domain.relations import get_relations
from hl_mem.experience.service import ExperienceService
from hl_mem.observability.audit import current_audit
from hl_mem.protocols import EmbedderProtocol, RerankerProtocol
from hl_mem.recall.policy import RecallIntent, route_recall_intent
from hl_mem.recall.recall_pipeline import hybrid_claims, matching_policies
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
    ) -> None:
        self.connection = connection
        self.embedder = embedder
        self.reranker = reranker

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
    ) -> dict[str, Any]:
        """执行混合召回并返回 claim、策略、证据及查询标识。"""
        query_id = query_id or new_id()
        selected_intent = intent or route_recall_intent(query, as_of)
        claims = hybrid_claims(
            ClaimRepository(self.connection),
            query,
            self.embedder.embed_one(query),
            limit,
            as_of,
            self.reranker,
            intent=selected_intent,
            known_as_of=known_as_of,
        )
        self._record_access(claims)
        self._record_feedback(claims, query_id)
        results = self._assemble_results(claims)
        observations = self._assemble_observations([claim["id"] for claim in claims])
        policies = matching_policies(ExperienceService(self.connection).list_policies("active"), query)
        response = {
            "results": results,
            "observations": observations,
            "policies": policies,
            "total": len(results),
            "query_id": query_id,
        }
        if context_mode == "packed":
            response["context"] = self._assemble_context(results, observations, policies, token_budget or 2000)
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
            if packed and used + cost > token_budget:
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

    def _assemble_results(self, claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
        evidence_repo = EvidenceRepository(self.connection)
        claim_repo = ClaimRepository(self.connection)
        results: list[dict[str, Any]] = []
        for claim in claims:
            evidence = [
                {"type": "event", "id": link["evidence_id"]}
                for link in evidence_repo.get_links_for_derived("claim", claim["id"])
            ]
            decoded = json.loads(claim["value_json"])
            text = (
                decoded.get("old_value")
                if isinstance(decoded, dict) and decoded.get("_type") == "superseded_value"
                else decoded
            )
            replacement = None
            if claim.get("superseded_by_id"):
                replacement_claim = claim_repo.get_claim(claim["superseded_by_id"])
                if replacement_claim:
                    replacement = {
                        "id": replacement_claim["id"],
                        "text": json.loads(replacement_claim["value_json"]),
                        "valid_from": replacement_claim["valid_from"],
                    }
            result = {
                "type": "claim",
                "id": claim["id"],
                "text": text,
                "status": claim["status"],
                "confidence": claim["confidence"],
                "valid_from": claim["valid_from"],
                "replacement": replacement,
                "evidence": evidence,
                "relations": get_relations(self.connection, claim["id"]),
            }
            if claim["status"] == "disputed" and claim.get("conflict_key"):
                rivals = self.connection.execute(
                    "SELECT id,value_json FROM claims WHERE conflict_key=? AND status='disputed' AND id!=?",
                    (claim["conflict_key"], claim["id"]),
                ).fetchall()
                result["conflicts"] = [dict(row) for row in rivals]
            results.append(result)
        return results
