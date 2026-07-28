"""Claim 时间区间与实体字段测试。"""

from __future__ import annotations

from hl_mem.application.ingest import IngestService
from hl_mem.application.recall import RecallService
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database

NOW = "2026-07-24T10:00:00+00:00"


def _store(connection, claim: ExtractedClaim) -> str:
    result = IngestService.store_extracted(
        connection,
        claim,
        {"id": f"event-{claim.value}", "actor_type": "user", "tenant_id": "default"},
        NOW,
        FakeEmbedder(8),
    )
    assert result.claim_id is not None
    return result.claim_id


def test_store_claim_with_occurred_range(tmp_path) -> None:
    connection = Database(tmp_path / "range.db").open()
    claim_id = _store(
        connection,
        ExtractedClaim(
            "计划",
            "参加会议",
            occurred_start="2026-08-01T09:00:00+00:00",
            occurred_end="2026-08-01T10:00:00+00:00",
        ),
    )

    row = connection.execute("SELECT occurred_start,occurred_end FROM claims WHERE id=?", (claim_id,)).fetchone()

    assert tuple(row) == ("2026-08-01T09:00:00+00:00", "2026-08-01T10:00:00+00:00")


def test_store_claim_with_entities(tmp_path) -> None:
    connection = Database(tmp_path / "entities.db").open()
    claim_id = _store(
        connection,
        ExtractedClaim("事实", "Alice 负责 hl_mem", entities=["Alice", "hl_mem"]),
    )

    claim = ClaimRepository(connection).get_claim(claim_id)

    assert claim["entities"] == ["Alice", "hl_mem"]


def test_invalid_subject_uses_legal_entity_replacement(tmp_path) -> None:
    """写入边界应使用 claim entities 中首个合法实体替换文件名 subject。"""
    connection = Database(tmp_path / "subject-replacement.db").open()
    claim_id = _store(
        connection,
        ExtractedClaim("事实", "hl_mem 使用 SQLite", subject="server.py", entities=["ALL_PROXY", "hl_mem 项目"]),
    )

    claim = ClaimRepository(connection).get_claim(claim_id)

    assert claim["subject_entity_id"] == "hl_mem"


def test_invalid_subject_without_replacement_uses_unknown_subject(tmp_path) -> None:
    """没有合法 entity 候选时应显式降级为 unknown_subject。"""
    connection = Database(tmp_path / "unknown-subject.db").open()
    claim_id = _store(
        connection,
        ExtractedClaim("事实", "环境变量已配置", subject="ALL_PROXY", entities=["server.py"]),
    )

    claim = ClaimRepository(connection).get_claim(claim_id)

    assert claim["subject_entity_id"] == "unknown_subject"


def test_recall_returns_temporal_entities(tmp_path) -> None:
    connection = Database(tmp_path / "recall.db").open()
    _store(
        connection,
        ExtractedClaim(
            "事实",
            "Alice 参加发布会",
            occurred_start="2026-08-01T09:00:00+00:00",
            occurred_end="2026-08-01T10:00:00+00:00",
            entities=["Alice", "发布会"],
        ),
    )

    result = RecallService(connection, FakeEmbedder(8)).recall("Alice 参加发布会", limit=1)["results"][0]

    assert result["occurred_start"] == "2026-08-01T09:00:00+00:00"
    assert result["occurred_end"] == "2026-08-01T10:00:00+00:00"
    assert result["entities"] == ["Alice", "发布会"]


def test_backward_compatible_null_fields(tmp_path) -> None:
    connection = Database(tmp_path / "compatible.db").open()
    claim_id = _store(connection, ExtractedClaim("事实", "旧格式 claim"))

    claim = ClaimRepository(connection).get_claim(claim_id)
    result = RecallService(connection, FakeEmbedder(8)).recall("旧格式 claim", limit=1)["results"][0]

    assert claim["occurred_start"] is None
    assert claim["occurred_end"] is None
    assert claim["entities"] is None
    assert "occurred_start" not in result
    assert "occurred_end" not in result
    assert "entities" not in result
