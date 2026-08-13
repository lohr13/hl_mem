from __future__ import annotations

import unittest
from unittest.mock import Mock

from hl_mem.domain.recall import RecallIntent
from hl_mem.recall.staged_pipeline import RecallContext, _preference_first, _rerank
from hl_mem.storage.claims import ClaimRepository


class _FixedReranker:
    def __init__(self, results: list[tuple[int, float]]) -> None:
        self._results = results

    def rerank(self, _query: str, _documents: list[str], top_n: int = 20) -> list[tuple[int, float]]:
        return self._results[:top_n]


class PreferenceFinalizationTest(unittest.TestCase):
    @staticmethod
    def _claim(claim_id: str, predicate: str, subject: str = "other") -> dict[str, str]:
        return {"id": claim_id, "predicate": predicate, "subject_entity_id": subject}

    def test_truncates_to_limit_preserving_global_ranking(self) -> None:
        """_preference_first is now a pure truncation; preference ordering is handled
        upstream by the score boost in _filter_and_score."""
        claims = [
            self._claim("relevant-fact", "事实"),
            self._claim("preference-1", "偏好"),
            self._claim("relevant-plan", "计划"),
            self._claim("preference-2", "偏好"),
        ]

        final = _preference_first(claims, 3, RecallIntent.PREFERENCE)

        self.assertEqual([claim["id"] for claim in final], ["relevant-fact", "preference-1", "relevant-plan"])

    def test_non_preference_intent_truncates_identically(self) -> None:
        claims = [
            self._claim("fact", "事实"),
            self._claim("preference", "偏好"),
            self._claim("plan", "计划"),
        ]

        final = _preference_first(claims, 2, RecallIntent.CURRENT_STATE)

        self.assertEqual([claim["id"] for claim in final], ["fact", "preference"])

    def test_limit_above_count_returns_all(self) -> None:
        claims = [
            self._claim("alice-1", "偏好", "Alice"),
            self._claim("fact", "事实", "user"),
        ]

        final = _preference_first(claims, 10, RecallIntent.PREFERENCE)

        self.assertEqual([claim["id"] for claim in final], ["alice-1", "fact"])


class RerankerPreferenceInteractionTest(unittest.TestCase):
    @staticmethod
    def _claim(claim_id: str, predicate: str) -> dict[str, str]:
        return {
            "id": claim_id,
            "predicate": predicate,
            "index_text": claim_id,
            "recorded_from": "2026-08-13T00:00:00+00:00",
        }

    @staticmethod
    def _features(claims: list[dict[str, str]]) -> dict[str, dict[str, float]]:
        return {
            claim["id"]: {
                "semantic": 0.5,
                "recency": 0.0,
                "access_frequency": 0.0,
                "confidence": 0.0,
                "importance": 0.0,
                "utility": 0.0,
            }
            for claim in claims
        }

    def _context(
        self,
        claims: list[dict[str, str]],
        reranked: list[tuple[int, float]],
    ) -> RecallContext:
        return RecallContext(
            repo=Mock(spec=ClaimRepository),
            query="用户偏好",
            query_blob=b"query",
            reranker=_FixedReranker(reranked),
            candidate_limit=len(claims),
            selected_intent=RecallIntent.PREFERENCE,
            feature_by_id=self._features(claims),
            ranked_claims=claims,
        )

    def test_preference_intent_preserves_reranker_order(self) -> None:
        claims = [self._claim("preference", "偏好"), self._claim("fact", "事实")]

        result = _rerank(self._context(claims, [(1, 0.9), (0, 0.8)]))

        self.assertEqual([claim["id"] for claim in result.ranked_result], ["fact", "preference"])

    def test_preference_omitted_by_reranker_is_not_reappended(self) -> None:
        claims = [self._claim("preference", "偏好"), self._claim("fact", "事实")]

        result = _rerank(self._context(claims, [(1, 0.9)]))

        self.assertEqual([claim["id"] for claim in result.ranked_result], ["fact"])


if __name__ == "__main__":
    unittest.main()
