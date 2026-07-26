"""召回折叠与 temporal 清理测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hl_mem.ingest.embedder import pack_vector
from hl_mem.recall.staged_pipeline import fold_similar_claims, hybrid_claims, RecallConfig
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.workers.decay import cleanup_stale_temporal_claims
from hl_mem.workers.ttl import expire_claims


class RecallFoldTemporalCleanupTest(unittest.TestCase):
    """验证重复折叠上限和 temporal 保守清理规则。"""

    def test_fold_keeps_highest_scored_claim_per_similar_group(self) -> None:
        vector = pack_vector([1.0, 0.0])
        claims = [
            {"id": "vt-high", "_score": 0.9, "embedding_dense": vector},
            {"id": "vt-low", "_score": 0.7, "embedding_dense": vector},
            {"id": "architecture-1", "_score": 0.8, "embedding_dense": pack_vector([0.0, 1.0])},
            {"id": "architecture-2", "_score": 0.6, "embedding_dense": pack_vector([0.1, 0.9])},
        ]
        folded = fold_similar_claims(claims, 0.95)
        self.assertEqual([claim["id"] for claim in folded], ["vt-high", "architecture-1"])

    def test_codex_and_architecture_queries_fold_duplicate_propositions(self) -> None:
        vector = pack_vector([1.0, 0.0])
        claims = [
            {
                "id": claim_id,
                "subject_entity_id": "Codex",
                "predicate": "事实",
                "value": value,
                "status": "active",
                "confidence": 0.9,
                "importance": 0.5,
                "scope": "permanent",
                "recorded_from": "2026-07-01T00:00:00+00:00",
                "embedding_dense": embedding,
            }
            for claim_id, value, embedding in (
                ("vt-1", "Windows VT 序列限制", vector),
                ("vt-2", "Windows VT 序列不支持", vector),
                ("app-1", "hl_mem application 层架构", pack_vector([0.0, 1.0])),
                ("app-2", "application 服务层结构", pack_vector([0.01, 0.99])),
                ("app-3", "application 分层结构", pack_vector([0.02, 0.98])),
            )
        ]

        class Repo:
            def search_claims_fts(self, *_args, **_kwargs):
                return claims

            def search_claims_vector(self, *_args, **_kwargs):
                return claims

            def helpful_rates(self, *_args, **_kwargs):
                return {}

        class Reranker:
            def rerank(self, _query, documents, top_n=20):
                return [(index, 1.0 - index * 0.1) for index in range(len(documents))][:top_n]

        config = RecallConfig(dedup_threshold=0.95)
        codex = hybrid_claims(Repo(), "Codex", vector, 10, None, Reranker(), recall_config=config)
        architecture = hybrid_claims(Repo(), "hl_mem 架构", vector, 10, None, Reranker(), recall_config=config)
        self.assertLessEqual(sum("VT" in str(item["value"]) for item in codex), 1)
        self.assertLessEqual(sum("application" in str(item["value"]) for item in architecture), 2)

    def test_temporal_cleanup_sets_expiry_or_promotes_permanent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = Database(Path(directory) / "temporal.db").open()
            repo = ClaimRepository(connection)
            base = {
                "namespace_key": "default",
                "predicate": "事实",
                "recorded_from": "2026-01-01T00:00:00+00:00",
                "status": "active",
                "scope": "temporal",
                "volatility": "stable",
            }
            repo.insert_claim({**base, "id": "state", "canonical_attribute": "state.service"})
            repo.insert_claim({**base, "id": "decision", "canonical_attribute": "fact.decision"})
            repo.insert_claim({**base, "id": "other", "canonical_attribute": "fact.capability"})

            result = cleanup_stale_temporal_claims(connection, "2026-07-26T00:00:00+00:00")

            self.assertEqual(result, {"expired_at_set": 1, "promoted": 1})
            state = connection.execute(
                "SELECT expires_at FROM claims WHERE id=?",
                ("state",),
            ).fetchone()[0]
            decision = connection.execute(
                "SELECT scope FROM claims WHERE id=?",
                ("decision",),
            ).fetchone()[0]
            self.assertEqual(state, "2026-04-01T00:00:00+00:00")
            self.assertEqual(decision, "permanent")
            self.assertEqual(
                connection.execute("SELECT expires_at FROM claims WHERE id=?", ("other",)).fetchone()[0],
                None,
            )
            self.assertEqual(expire_claims(connection, "2026-07-26T00:00:00+00:00"), {"expired": 1})
            connection.close()


if __name__ == "__main__":
    unittest.main()
