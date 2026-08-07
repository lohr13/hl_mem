"""NumPy 分批精确 cosine 扫描测试。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from hl_mem.core.vector import batch_cosine_similarity, cosine_similarity, pack_vector
from hl_mem.errors import ConfigurationError
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database


def test_batch_results_match_scalar_cosine_with_float32_tolerance() -> None:
    """批量计算结果应在 float32 容差内匹配标量实现。"""
    query = pack_vector([0.5, -1.25, 3.0, 0.125])
    targets = [
        pack_vector([0.5, -1.25, 3.0, 0.125]),
        pack_vector([-2.0, 1.0, 0.25, 4.0]),
        pack_vector([1.5, 0.0, -3.0, 2.0]),
    ]

    assert batch_cosine_similarity(query, targets) == pytest.approx(
        [cosine_similarity(query, target) for target in targets],
        abs=1e-6,
    )


def test_batch_handles_empty_targets_and_zero_vectors() -> None:
    """空集合返回空结果，查询或目标零向量得分保持零。"""
    query = pack_vector([1.0, 0.0])
    zero = pack_vector([0.0, 0.0])

    assert batch_cosine_similarity(query, []) == []
    assert batch_cosine_similarity(query, [zero]) == [0.0]
    assert batch_cosine_similarity(zero, [query, zero]) == [0.0, 0.0]


@pytest.mark.parametrize(
    ("query", "targets", "message"),
    [
        (pack_vector([1.0, 2.0]), [pack_vector([1.0])], "embedding dimensions differ"),
        (b"\x00", [], "embedding BLOB length must be divisible by four"),
        (pack_vector([1.0]), [b"\x00"], "embedding BLOB length must be divisible by four"),
    ],
)
def test_batch_rejects_invalid_dimensions(query: bytes, targets: list[bytes], message: str) -> None:
    """维度不一致或无效 BLOB 应保持具体错误语义。"""
    with pytest.raises(ValueError, match=message):
        batch_cosine_similarity(query, targets)


def test_vector_search_uses_claim_id_for_tied_scores(tmp_path) -> None:
    """并列分数使用 claim_id 进行确定性排序。"""
    connection = Database(tmp_path / "stable-vector.db").open()
    repository = ClaimRepository(connection, vector_batch_size=1)
    base = {
        "namespace_key": "default",
        "subject_entity_id": "subject",
        "predicate": "fact",
        "recorded_from": "2026-01-01T00:00:00+00:00",
        "status": "active",
    }
    repository.insert_claim({**base, "id": "claim-b", "value": "b", "embedding_dense": pack_vector([1.0, 0.0])})
    repository.insert_claim({**base, "id": "claim-a", "value": "a", "embedding_dense": pack_vector([2.0, 0.0])})

    assert [claim["id"] for claim in repository.search_claims_vector(pack_vector([1.0, 0.0]))] == [
        "claim-a",
        "claim-b",
    ]


def test_vector_search_light_scan_oversamples_before_materializing_rows(tmp_path) -> None:
    """轻量扫描应回表足量候选，以补偿完整可见性过滤。"""
    database = Database(tmp_path / "two-stage-vector.db")
    connection = database.open()
    try:
        repository = ClaimRepository(connection, vector_batch_size=7)
        base = {
            "namespace_key": "default",
            "subject_entity_id": "subject",
            "predicate": "fact",
            "recorded_from": "2026-01-01T00:00:00+00:00",
            "status": "active",
            "embedding_dense": pack_vector([1.0, 0.0]),
        }
        for index in range(52):
            repository.insert_claim(
                {
                    **base,
                    "id": f"claim-{index:02d}",
                    "value": f"value-{index:02d}",
                    "recorded_to": "2026-02-01T00:00:00+00:00" if index < 50 else None,
                }
            )

        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        results = repository.search_claims_vector(
            pack_vector([1.0, 0.0]),
            limit=2,
            as_of="2026-08-01T00:00:00+00:00",
            intent="current_state",
            known_as_of="2026-08-01T00:00:00+00:00",
        )
        connection.set_trace_callback(None)

        selects = [statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]
        assert selects[0].startswith("SELECT id, embedding_dense FROM claims")
        assert selects[1].startswith("SELECT * FROM claims WHERE id IN (")
        assert len(selects) == 2
        assert [(claim["id"], claim["value"], claim["_score"]) for claim in results] == [
            ("claim-50", "value-50", 1.0),
            ("claim-51", "value-51", 1.0),
        ]
    finally:
        database.close()


def test_vector_search_materializes_later_batches_until_limit(tmp_path) -> None:
    """首批候选均不可见时应继续回表，直到达到 limit 或候选耗尽。"""
    database = Database(tmp_path / "two-stage-vector-pagination.db")
    connection = database.open()
    try:
        repository = ClaimRepository(connection, vector_batch_size=7)
        base = {
            "namespace_key": "default",
            "subject_entity_id": "subject",
            "predicate": "fact",
            "recorded_from": "2026-01-01T00:00:00+00:00",
            "status": "active",
            "embedding_dense": pack_vector([1.0, 0.0]),
        }
        for index in range(54):
            repository.insert_claim(
                {
                    **base,
                    "id": f"claim-{index:02d}",
                    "value": f"value-{index:02d}",
                    "recorded_to": "2026-02-01T00:00:00+00:00" if index < 52 else None,
                }
            )

        results = repository.search_claims_vector(
            pack_vector([1.0, 0.0]),
            limit=2,
            as_of="2026-08-01T00:00:00+00:00",
            intent="current_state",
            known_as_of="2026-08-01T00:00:00+00:00",
        )

        assert [claim["id"] for claim in results] == ["claim-52", "claim-53"]
    finally:
        database.close()


def test_vector_search_zero_limit_returns_empty(tmp_path) -> None:
    """零上限应保持空结果，且无需执行向量扫描。"""
    database = Database(tmp_path / "zero-limit-vector.db")
    connection = database.open()
    try:
        repository = ClaimRepository(connection)
        repository.insert_claim(
            {
                "id": "claim",
                "namespace_key": "default",
                "subject_entity_id": "subject",
                "predicate": "fact",
                "value": "value",
                "recorded_from": "2026-01-01T00:00:00+00:00",
                "status": "active",
                "embedding_dense": pack_vector([1.0, 0.0]),
            }
        )
        statements: list[str] = []
        connection.set_trace_callback(statements.append)

        assert repository.search_claims_vector(pack_vector([1.0, 0.0]), limit=0) == []
        assert not any(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    finally:
        database.close()


def test_different_batch_sizes_produce_identical_results() -> None:
    """batch size 仅控制临时矩阵，不应改变分数。"""
    query = pack_vector([1.0, -2.0, 0.5])
    targets = [pack_vector([float(index), 1.0, -0.25]) for index in range(37)]

    assert batch_cosine_similarity(query, targets, batch_size=1) == pytest.approx(
        batch_cosine_similarity(query, targets, batch_size=16),
        abs=1e-7,
    )


def test_large_batch_does_not_crash() -> None:
    """一千条以上向量应能通过多个批次完成扫描。"""
    query = pack_vector([1.0, 2.0, 3.0, 4.0])
    targets = [pack_vector([float(index % 7), 2.0, -1.0, 0.5]) for index in range(1_025)]

    scores = batch_cosine_similarity(query, targets, batch_size=128)

    assert len(scores) == len(targets)


def test_vector_batch_size_settings_contract() -> None:
    """批大小应进入快照并拒绝非正整数。"""
    settings = Settings(vector_batch_size=64)

    assert settings.vector_batch_size == 64
    assert settings.snapshot()["vector_batch_size"] == 64

    with pytest.raises(ConfigurationError, match=r"recall\.vector_batch_size must be positive"):
        replace(Settings.for_test(), vector_batch_size=0).validate()
