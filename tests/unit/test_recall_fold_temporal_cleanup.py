"""召回折叠与 temporal 清理测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from hl_mem.ingest.embedder import pack_vector
from hl_mem.recall.staged_pipeline import (
    RecallConfig,
    _confirmed_equivalent_pairs,
    fold_similar_claims,
    hybrid_claims,
)
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
        common = {
            "namespace_key": "default",
            "subject_entity_id": "Codex",
            "predicate": "事实",
            "canonical_attribute": "fact.other",
            "canonical_slot": None,
            "qualifiers": {},
            "status": "active",
            "valid_from": "2026-01-01T00:00:00+00:00",
            "valid_to": None,
        }
        claims = [
            {
                **common,
                "id": "vt-high",
                "value": "Windows VT 序列不支持部分控制代码",
                "_score": 0.9,
                "embedding_dense": vector,
            },
            {
                **common,
                "id": "vt-low",
                "value": "The Windows VT 序列不支持部分控制代码。",
                "_score": 0.7,
                "embedding_dense": vector,
            },
            {
                **common,
                "id": "architecture-1",
                "value": "hl_mem application 服务层采用分层架构",
                "_score": 0.8,
                "embedding_dense": pack_vector([0.0, 1.0]),
            },
            {
                **common,
                "id": "architecture-2",
                "value": "The hl_mem application 服务层采用分层架构。",
                "_score": 0.6,
                "embedding_dense": pack_vector([0.1, 0.9]),
            },
        ]
        folded = fold_similar_claims(claims, 0.95)
        self.assertEqual([claim["id"] for claim in folded], ["vt-high", "architecture-1"])

    def test_fold_preserves_swapped_cjk_entity_roles(self) -> None:
        vector = pack_vector([1.0, 0.0])
        common = {
            "namespace_key": "default",
            "subject_entity_id": "user",
            "predicate": "事实",
            "canonical_attribute": "fact.other",
            "canonical_slot": None,
            "qualifiers": {},
            "entities": ["user", "张三", "李四"],
            "status": "active",
            "valid_from": "2026-01-01T00:00:00+00:00",
            "valid_to": None,
            "embedding_dense": vector,
        }
        claims = [
            {
                **common,
                "id": "zhang-to-li",
                "value": "用户把张三介绍给李四，并要求他们共同完成本周的详细项目状态报告，随后还需要核对每项进度、风险和下周计划，并把所有结论同步到团队共享的项目文档中。",
                "_score": 0.9,
            },
            {
                **common,
                "id": "li-to-zhang",
                "value": "用户把李四介绍给张三，并要求他们共同完成本周的详细项目状态报告，随后还需要核对每项进度、风险和下周计划，并把所有结论同步到团队共享的项目文档中。",
                "_score": 0.8,
            },
            {
                **common,
                "id": "redis-to-mysql",
                "value": "用户将Redis迁移到MySQL，并要求团队完成详细兼容性检查、风险清单和回滚计划，再把所有结论同步到共享项目文档中。",
                "entities": ["user", "Redis", "MySQL"],
                "_score": 0.7,
            },
            {
                **common,
                "id": "mysql-to-redis",
                "value": "用户将MySQL迁移到Redis，并要求团队完成详细兼容性检查、风险清单和回滚计划，再把所有结论同步到共享项目文档中。",
                "entities": ["user", "Redis", "MySQL"],
                "_score": 0.6,
            },
        ]

        folded = fold_similar_claims(claims, 0.95)

        self.assertEqual(
            [claim["id"] for claim in folded],
            ["zhang-to-li", "li-to-zhang", "redis-to-mysql", "mysql-to-redis"],
        )

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
            {
                **base,
                "id": "likes",
                "predicate": "喜欢",
                "status": "active",
                "_score": 0.9,
            },
            {
                **base,
                "id": "dislikes",
                "predicate": "不喜欢",
                "status": "active",
                "_score": 0.8,
            },
            {
                **base,
                "id": "disputed",
                "predicate": "喜欢",
                "status": "disputed",
                "_score": 0.7,
            },
        ]

        folded = fold_similar_claims(claims, 0.95)

        self.assertEqual([claim["id"] for claim in folded], ["likes", "dislikes", "disputed"])

    def test_fold_preserves_different_ports_qualifiers_and_disjoint_valid_times(
        self,
    ) -> None:
        """端口、限定条件或有效时间不兼容时，即使向量相同也不得折叠。"""
        vector = pack_vector([1.0, 0.0])
        base = {
            "namespace_key": "default",
            "subject_entity_id": "service",
            "canonical_slot": "state.service_port",
            "predicate": "端口",
            "status": "active",
            "embedding_dense": vector,
        }
        claims = [
            {
                **base,
                "id": "production-8200",
                "value": "端口 8200",
                "qualifiers": {"environment": "production"},
                "valid_from": "2026-01-01T00:00:00+00:00",
                "valid_to": "2026-02-01T00:00:00+00:00",
                "_score": 0.9,
            },
            {
                **base,
                "id": "production-8080",
                "value": "端口 8080",
                "qualifiers": {"environment": "production"},
                "valid_from": "2026-01-01T00:00:00+00:00",
                "valid_to": "2026-02-01T00:00:00+00:00",
                "_score": 0.8,
            },
            {
                **base,
                "id": "staging-8200",
                "value": "端口 8200",
                "qualifiers": {"environment": "staging"},
                "valid_from": "2026-01-01T00:00:00+00:00",
                "valid_to": "2026-02-01T00:00:00+00:00",
                "_score": 0.7,
            },
            {
                **base,
                "id": "production-8200-later",
                "value": "服务端口是 8200",
                "qualifiers": {"environment": "production"},
                "valid_from": "2026-03-01T00:00:00+00:00",
                "valid_to": None,
                "_score": 0.6,
            },
        ]

        folded = fold_similar_claims(claims, 0.95)

        self.assertEqual(
            [claim["id"] for claim in folded],
            [
                "production-8200",
                "production-8080",
                "staging-8200",
                "production-8200-later",
            ],
        )

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
                "value": "用户偏好深色主题",
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

    def test_fold_preserves_ordered_atoms_and_negation(self) -> None:
        vector = pack_vector([1.0, 0.0])
        base = {
            "namespace_key": "default",
            "subject_entity_id": "user",
            "predicate": "fact",
            "canonical_attribute": "fact.other",
            "canonical_slot": None,
            "qualifiers": {},
            "status": "active",
            "valid_from": "2026-01-01T00:00:00+00:00",
            "valid_to": None,
            "embedding_dense": vector,
        }
        claims = [
            {
                **base,
                "id": "ordered-one",
                "value": "Take 1 tablet at 2 pm after breakfast every weekday and record the result.",
                "_score": 0.9,
            },
            {
                **base,
                "id": "ordered-two",
                "value": "Take 2 tablets at 1 pm after breakfast every weekday and record the result.",
                "_score": 0.8,
            },
            {
                **base,
                "id": "allows",
                "value": "The policy does allow this operation under normal production conditions.",
                "_score": 0.7,
            },
            {
                **base,
                "id": "denies",
                "value": "The policy doesn't allow this operation under normal production conditions.",
                "_score": 0.6,
            },
        ]

        folded = fold_similar_claims(claims, 0.95)

        self.assertEqual([claim["id"] for claim in folded], ["ordered-one", "ordered-two", "allows", "denies"])

    def test_fold_uses_confirmed_equivalent_pair_across_subjects(self) -> None:
        base = {
            "namespace_key": "default",
            "predicate": "fact",
            "canonical_attribute": "fact.other",
            "canonical_slot": None,
            "qualifiers": {},
            "status": "active",
            "valid_from": "2026-01-01T00:00:00+00:00",
            "valid_to": None,
        }
        claims = [
            {
                **base,
                "id": "high",
                "subject_entity_id": "user",
                "value": "User's tank is 20 gallons",
                "_score": 0.9,
            },
            {
                **base,
                "id": "low",
                "subject_entity_id": "user's tank",
                "value": "The user's tank size is 20 gallons.",
                "_score": 0.8,
            },
        ]

        folded = fold_similar_claims(
            claims,
            0.95,
            equivalent_pairs=[("high", "low", 0.97)],
        )

        self.assertEqual([claim["id"] for claim in folded], ["high"])
        self.assertEqual(folded[0]["_equivalent_claim_ids"], ["low"])

    def test_fold_dynamically_collapses_safe_cross_subject_near_copy(self) -> None:
        vector = pack_vector([1.0, 0.0])
        base = {
            "namespace_key": "default",
            "predicate": "fact",
            "canonical_attribute": "fact.other",
            "canonical_slot": None,
            "qualifiers": {},
            "status": "active",
            "valid_from": "2026-01-01T00:00:00+00:00",
            "valid_to": None,
            "embedding_dense": vector,
        }
        claims = [
            {
                **base,
                "id": "high",
                "subject_entity_id": "user",
                "value": "User's tank is 20 gallons",
                "_score": 0.9,
            },
            {
                **base,
                "id": "low",
                "subject_entity_id": "user's tank",
                "value": "The user's tank size is 20 gallons.",
                "_score": 0.8,
            },
            {
                **base,
                "id": "different-number",
                "subject_entity_id": "user's other tank",
                "value": "The user's tank size is 30 gallons.",
                "_score": 0.7,
            },
        ]

        folded = fold_similar_claims(claims, 0.95)

        self.assertEqual([claim["id"] for claim in folded], ["high", "different-number"])
        self.assertEqual(folded[0]["_equivalent_claim_ids"], ["low"])

    def test_recall_loads_only_deterministically_confirmed_equivalent_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = Database(Path(directory) / "equivalent-pairs.db").open()
            repo = ClaimRepository(connection)
            common = {
                "namespace_key": "default",
                "predicate": "fact",
                "recorded_from": "2026-01-01T00:00:00+00:00",
                "status": "active",
            }
            for claim_id in ("left", "right", "llm-right"):
                repo.insert_claim({**common, "id": claim_id, "value": claim_id})
            connection.executemany(
                "INSERT INTO dedup_pairs("
                "id,pair_key,left_claim_id,right_claim_id,similarity,decision,judge_reason,created_at"
                ") VALUES (?,?,?,?,?,?,?,?)",
                [
                    (
                        "safe",
                        "safe",
                        "left",
                        "right",
                        0.97,
                        "equivalent",
                        "deterministic_near_copy_v1",
                        common["recorded_from"],
                    ),
                    ("llm", "llm", "left", "llm-right", 0.99, "equivalent", "llm_review", common["recorded_from"]),
                ],
            )
            connection.commit()

            pairs = _confirmed_equivalent_pairs(connection, ["left", "right", "llm-right"], 0.95)
            connection.close()

        self.assertEqual(pairs, [("left", "right", 0.97)])

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
                ("vt-1", "Windows VT 序列不支持部分控制代码", vector),
                ("vt-2", "The Windows VT 序列不支持部分控制代码。", vector),
                ("app-1", "hl_mem application 服务层采用分层架构", pack_vector([0.0, 1.0])),
                ("app-2", "The hl_mem application 服务层采用分层架构。", pack_vector([0.01, 0.99])),
                ("app-3", "hl_mem application 服务层采用清晰分层架构", pack_vector([0.02, 0.98])),
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

            result = cleanup_stale_temporal_claims(
                connection,
                "2026-07-26T00:00:00+00:00",
                age_days=30,
                expiry_days=90,
            )

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
            self.assertEqual(
                expire_claims(
                    connection,
                    "2026-07-26T00:00:00+00:00",
                    feedback_lifecycle_mode="observe",
                    slot_short_ttl_seconds=86400,
                ),
                {"expired": 1},
            )
            connection.close()

    def test_ttl_scan_uses_180_day_candidate_window(self) -> None:
        """TTL 查询应先用最大 feedback bonus 窗口缩小 expires_at 候选集。"""
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "ttl-window.db")
            connection = database.open()
            try:
                statements: list[str] = []
                connection.set_trace_callback(statements.append)

                expire_claims(
                    connection,
                    "2026-07-26T00:00:00+00:00",
                    feedback_lifecycle_mode="observe",
                    slot_short_ttl_seconds=86400,
                )

                normalized = [" ".join(statement.split()).lower() for statement in statements]
                self.assertTrue(
                    any("c.expires_at<='2027-01-22t00:00:00+00:00'" in statement for statement in normalized),
                    normalized,
                )
            finally:
                database.close()

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

    def test_temporal_cleanup_ignores_legacy_volatility(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = Database(Path(directory) / "temporal-volatility.db").open()
            try:
                ClaimRepository(connection).insert_claim(
                    {
                        "id": "state",
                        "namespace_key": "default",
                        "predicate": "事实",
                        "recorded_from": "2026-01-01T00:00:00+00:00",
                        "status": "active",
                        "scope": "temporal",
                        "volatility": "ephemeral",
                        "canonical_attribute": "state.service",
                    }
                )

                self.assertEqual(
                    cleanup_stale_temporal_claims(
                        connection,
                        "2026-07-26T00:00:00+00:00",
                        age_days=30,
                        expiry_days=90,
                    ),
                    {"expired_at_set": 1, "promoted": 0},
                )
                self.assertEqual(
                    connection.execute("SELECT expires_at FROM claims WHERE id='state'").fetchone()[0],
                    "2026-04-01T00:00:00+00:00",
                )
            finally:
                connection.close()

    def test_temporal_cleanup_rereads_classification_after_write_lock(self) -> None:
        """第二连接在加锁前重分类时，清理不得应用旧 attribute 快照。"""
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
                        "UPDATE claims SET canonical_attribute=? WHERE id=?",
                        ("fact.capability", "claim"),
                    )
                    concurrent_connection.commit()

                result = cleanup_stale_temporal_claims(
                    _BeforeBeginConnection(cleanup_connection, reclassify),
                    "2026-07-26T00:00:00+00:00",
                    age_days=30,
                    expiry_days=90,
                )

                row = cleanup_connection.execute(
                    "SELECT canonical_attribute,expires_at FROM claims WHERE id=?",
                    ("claim",),
                ).fetchone()
                self.assertEqual(result, {"expired_at_set": 0, "promoted": 0})
                self.assertEqual(tuple(row), ("fact.capability", None))
            finally:
                database.close()


if __name__ == "__main__":
    unittest.main()
