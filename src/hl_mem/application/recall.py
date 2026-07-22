"""记忆召回应用服务。执行 FTS + 向量 + reranker 混合召回，管理访问记录和反馈。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from hl_mem.application.ingest import new_id
from hl_mem.config import RECALL_DEFAULT_LIMIT
from hl_mem.experience.service import ExperienceService
from hl_mem.observability.audit import current_audit
from hl_mem.recall.policy import RecallIntent, route_recall_intent
from hl_mem.recall.recall_pipeline import hybrid_claims, matching_policies
from hl_mem.storage.repository import ClaimRepository, EvidenceRepository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RecallService:
    """记忆召回应用服务。"""

    def __init__(self, connection: Any, embedder: Any, reranker: Any = None) -> None:
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
        policies = matching_policies(ExperienceService(self.connection).list_policies("active"), query)
        return {
            "results": results,
            "observations": [],
            "policies": policies,
            "total": len(results),
            "query_id": query_id,
        }

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
            results.append(
                {
                    "type": "claim",
                    "id": claim["id"],
                    "text": text,
                    "status": claim["status"],
                    "confidence": claim["confidence"],
                    "valid_from": claim["valid_from"],
                    "replacement": replacement,
                    "evidence": evidence,
                }
            )
        return results
