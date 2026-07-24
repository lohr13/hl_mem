"""为 trigram FTS 提供不依赖生产环境的确定性行为门禁。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database

NOW = "2026-07-24T00:00:00+00:00"


@pytest.fixture
def claim_repository(tmp_path: Path) -> Iterator[ClaimRepository]:
    """在临时数据库中写入带确定性 FakeEmbedder 向量的测试 claims。"""
    database = Database(tmp_path / "fts-trigram.db")
    connection = database.open()
    repository = ClaimRepository(connection)
    embedder = FakeEmbedder(8)
    for claim_id, text in (
        ("chinese", "记忆系统架构设计"),
        ("english", "SQLite FTS5 trigram"),
        ("mixed", "使用 Codex CLI 辅助开发"),
    ):
        assert repository.insert_claim(
            {
                "id": claim_id,
                "predicate": "描述",
                "value": text,
                "recorded_from": NOW,
                "status": "active",
                "embedding_dense": embedder.embed_one(text),
                "embedding_model": "fake",
                "embedding_dim": 8,
            }
        )
    yield repository
    database.close()


def _result_ids(repository: ClaimRepository, query: str) -> list[str]:
    """返回 FTS 查询命中的 claim 标识。"""
    return [claim["id"] for claim in repository.search_claims_fts(query)]


def test_chinese_substring_match(claim_repository: ClaimRepository) -> None:
    """中文连续子串“记忆系统”应命中更长文本。"""
    assert _result_ids(claim_repository, "记忆系统") == ["chinese"]


def test_english_phrase_match(claim_repository: ClaimRepository) -> None:
    """英文片段 FTS5 应命中英文 claim。"""
    assert _result_ids(claim_repository, "FTS5") == ["english"]


def test_mixed_chinese_english(claim_repository: ClaimRepository) -> None:
    """中英混合文本中的 Codex 应可检索。"""
    assert _result_ids(claim_repository, "Codex") == ["mixed"]


def test_short_query_returns_empty(claim_repository: ClaimRepository) -> None:
    """少于三个 Unicode 字符的 trigram 查询应返回空结果。"""
    assert _result_ids(claim_repository, "FT") == []


@pytest.mark.parametrize("query", ["C++", "foo-bar"])
def test_special_characters_quoted(claim_repository: ClaimRepository, query: str) -> None:
    """FTS 特殊字符应被 phrase quoting 安全处理且不抛异常。"""
    assert _result_ids(claim_repository, query) == []


def test_empty_query_returns_empty(claim_repository: ClaimRepository) -> None:
    """空白查询应安全返回空结果。"""
    assert _result_ids(claim_repository, "") == []
