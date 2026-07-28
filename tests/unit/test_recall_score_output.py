"""召回结果 score 输出契约测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hl_mem.api.schemas import ClaimOutput
from hl_mem.application.recall import RecallService
from hl_mem.ingest.embedder import FakeEmbedder, pack_vector
from hl_mem.recall.ranking import blend_reranker_score
from hl_mem.recall.recall_pipeline import hybrid_claims
from hl_mem.storage.database import Database


class _Repo:
    def __init__(self) -> None:
        self.claims = [
            {
                "id": claim_id,
                "subject_entity_id": "hl_mem",
                "predicate": "事实",
                "value": value,
                "status": "active",
                "confidence": 0.9,
                "importance": 0.5,
                "scope": "permanent",
                "recorded_from": "2026-07-01T00:00:00+00:00",
                "embedding_dense": vector,
            }
            for claim_id, value, vector in (
                ("first", "第一条", pack_vector([1.0, 0.0])),
                ("second", "第二条", pack_vector([0.0, 1.0])),
            )
        ]

    def search_claims_fts(self, *_args, **_kwargs):
        return self.claims

    def search_claims_vector(self, *_args, **_kwargs):
        return self.claims

    def helpful_rates(self, *_args, **_kwargs):
        return {}


class _Reranker:
    def rerank(self, _query, _documents, top_n=20):
        return [(0, 0.8), (1, 0.2)][:top_n]


class RecallScoreOutputTest(unittest.TestCase):
    """验证公开 score 使用管线的最终分数。"""

    def test_claim_output_requires_positive_score_and_assembly_preserves_it(
        self,
    ) -> None:
        claims = hybrid_claims(
            _Repo(),
            "查询",
            pack_vector([1.0, 0.0]),
            2,
            None,
            _Reranker(),
            now="2026-07-26T00:00:00+00:00",
        )
        expected = blend_reranker_score(0.8, claims[0]["_features"])
        with tempfile.TemporaryDirectory() as directory:
            connection = Database(Path(directory) / "score.db").open()
            result = RecallService(connection, FakeEmbedder(2))._assemble_results(
                [{**claims[0], "valid_from": None, "superseded_by_id": None}]
            )[0]
            connection.close()
        self.assertGreater(result["score"], 0.0)
        self.assertAlmostEqual(result["score"], expected)
        self.assertEqual(ClaimOutput.model_validate(result).score, result["score"])

    def test_without_reranker_score_equals_pre_score(self) -> None:
        claims = hybrid_claims(
            _Repo(),
            "查询",
            pack_vector([1.0, 0.0]),
            1,
            None,
            now="2026-07-26T00:00:00+00:00",
        )
        self.assertAlmostEqual(claims[0]["_score"], claims[0]["_pre_score"])


if __name__ == "__main__":
    unittest.main()
