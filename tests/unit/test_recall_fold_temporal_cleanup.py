"""召回折叠与 temporal 清理测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from hl_mem.ingest.embedder import pack_vector
from hl_mem.recall.staged_pipeline import RecallConfig, fold_similar_claims, hybrid_claims
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.workers.decay import cleanup_stale_temporal_claims
from hl_mem.workers.ttl import expire_claims


class _BeforeBeginConnection:
    """在首次写事务加锁前注入另一个真实连接的并发提交。"""

    def __init__(self, connection: Any, before_begin: Any) -> None:
        self._connection = connection
        self._before_begin = before_begin
        self._injected = False

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> Any:
        if sql == "BEGIN IMMEDIATE" and not self._injected:
            self._injected = True
            self._before_begin()
        return self._connection.execute(sql, parameters)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


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

    def test_fold_preserves_different_predicates_and_disputed_claims(self) -> None:
        """高相似向量不得隐藏相反 predicate 或 disputed 候选。"""
        vector = pack_vector([1.0, 0.0])
        base = {
            "namespace_key": "default",
            "subject_entity_id": "user",
            "canonical_slot": None,
            "embedding_dense": vector,
        }
        claims = [
            {**base, "id": "likes", "predicate": "喜欢", "status": "active", "_score": 0.9},
            {**base, "id": "dislikes", "predicate": "不喜欢", "status": "active", "_score": 0.8},
            {**base, "id": "disputed", "predicate": "喜欢", "status": "disputed", "_score": 0.7},
        ]

        folded = fold_similar_claims(claims, 0.95)

        self.assertEqual([claim["id"] for claim in folded], ["likes", "dislikes", "disputed"])

    def test_fold_decodes_only_top_candidate_window_once_each(self) -> None:
        """大候选集只解码配置窗口内的向量，且每条至多一次。"""
        vector = pack_vector([1.0, 0.0])
        claims = [
            {
                "id": f"claim-{index}",
                "namespace_key": "default",
                "subject_entity_id": "user",
                "canonical_slot": "preference.general",
                "predicate": "偏好",
                "status": "active",
                "_score": 1.0 - index / 1_000,
                "embedding_dense": vector,
            }
            for index in range(150)
        ]

        from hl_mem.core.vector import normalized_vector

        with patch(
            "hl_mem.recall.staged_pipeline.normalized_vector",
            wraps=normalized_vector,
        ) as decode:
            folded = fold_similar_claims(claims, 0.95, candidate_limit=100)

        self.assertEqual(decode.call_count, 100)
        self.assertEqual(len(folded), 51)

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

    def test_temporal_cleanup_uses_configured_age_and_expiry_days(self) -> None:
        """自定义年龄门槛和过期周期必须改变清理结果。"""
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "temporal-settings.db")
            connection = database.open()
            try:
                ClaimRepository(connection).insert_claim(
                    {
                        "id": "state",
                        "namespace_key": "default",
                        "predicate": "事实",
                        "recorded_from": "2026-01-01T00:00:00+00:00",
                        "status": "active",
                        "scope": "temporal",
                        "volatility": "stable",
                        "canonical_attribute": "state.service",
                    }
                )
                self.assertEqual(
                    cleanup_stale_temporal_claims(
                        connection,
                        "2026-07-26T00:00:00+00:00",
                        age_days=300,
                        expiry_days=120,
                    ),
                    {"expired_at_set": 0, "promoted": 0},
                )
                self.assertEqual(
                    cleanup_stale_temporal_claims(
                        connection,
                        "2026-07-26T00:00:00+00:00",
                        age_days=30,
                        expiry_days=120,
                    ),
                    {"expired_at_set": 1, "promoted": 0},
                )
                expires_at = connection.execute(
                    "SELECT expires_at FROM claims WHERE id=?",
                    ("state",),
                ).fetchone()[0]
                self.assertEqual(expires_at, "2026-05-01T00:00:00+00:00")
            finally:
                database.close()

    def test_temporal_cleanup_rereads_classification_after_write_lock(self) -> None:
        """第二连接在加锁前重分类时，清理不得应用旧 attribute/volatility 快照。"""
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "temporal-concurrency.db")
            cleanup_connection = database.open()
            concurrent_connection = database.open()
            try:
                ClaimRepository(cleanup_connection).insert_claim(
                    {
                        "id": "claim",
                        "namespace_key": "default",
                        "predicate": "事实",
                        "recorded_from": "2026-01-01T00:00:00+00:00",
                        "status": "active",
                        "scope": "temporal",
                        "volatility": "stable",
                        "canonical_attribute": "state.service",
                    }
                )

                def reclassify() -> None:
                    concurrent_connection.execute(
                        "UPDATE claims SET canonical_attribute=?,volatility=? WHERE id=?",
                        ("fact.capability", "ephemeral", "claim"),
                    )
                    concurrent_connection.commit()

                result = cleanup_stale_temporal_claims(
                    _BeforeBeginConnection(cleanup_connection, reclassify),
                    "2026-07-26T00:00:00+00:00",
                )

                row = cleanup_connection.execute(
                    "SELECT canonical_attribute,volatility,expires_at FROM claims WHERE id=?",
                    ("claim",),
                ).fetchone()
                self.assertEqual(result, {"expired_at_set": 0, "promoted": 0})
                self.assertEqual(tuple(row), ("fact.capability", "ephemeral", None))
            finally:
                database.close()


if __name__ == "__main__":
    unittest.main()
