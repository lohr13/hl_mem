"""为 tokenized FTS v2 和保留的 legacy query builder 提供确定性门禁。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.storage._shared import build_fts_trigram_fallback_query
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
        ("gpu", "用户的 GPU 型号是 RTX 4090"),
        ("strict-exact", "完整严格查询"),
        ("strict-overlap", "严格查询"),
        ("visible-overlap", "严格命中"),
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
    invisible_text = "不可见严格命中"
    assert repository.insert_claim(
        {
            "id": "invisible-exact",
            "predicate": "描述",
            "value": invisible_text,
            "recorded_from": NOW,
            "status": "active",
            "expires_at": "2000-01-01T00:00:00+00:00",
            "embedding_dense": embedder.embed_one(invisible_text),
            "embedding_model": "fake",
            "embedding_dim": 8,
        }
    )
    yield repository
    database.close()


def _result_ids(repository: ClaimRepository, query: str) -> list[str]:
    """返回 FTS 查询命中的 claim 标识。"""
    return [claim["id"] for claim in repository.search_claims_fts(query)]


def _traced_result_ids(repository: ClaimRepository, query: str) -> tuple[list[str], int]:
    """返回查询结果及实际执行的 claims FTS MATCH 次数。"""
    statements: list[str] = []
    repository.connection.set_trace_callback(statements.append)
    try:
        result_ids = _result_ids(repository, query)
    finally:
        repository.connection.set_trace_callback(None)
    match_count = sum("FROM claims_fts" in statement and " MATCH " in statement for statement in statements)
    return result_ids, match_count


def test_chinese_substring_match(claim_repository: ClaimRepository) -> None:
    """中文连续子串“记忆系统”应命中更长文本。"""
    assert _result_ids(claim_repository, "记忆系统") == ["chinese"]


def test_english_phrase_match(claim_repository: ClaimRepository) -> None:
    """英文片段 FTS5 应命中英文 claim。"""
    assert _result_ids(claim_repository, "FTS5") == ["english"]


def test_mixed_chinese_english(claim_repository: ClaimRepository) -> None:
    """中英混合文本中的 Codex 应可检索。"""
    assert _result_ids(claim_repository, "Codex") == ["mixed"]


def test_tokenized_fts_answers_cjk_and_latin_in_one_match(claim_repository: ClaimRepository) -> None:
    """自然中英混合问句应由统一 lexicalizer 在单次 v2 MATCH 中召回。"""
    assert _traced_result_ids(claim_repository, "用户的GPU型号是什么") == (["gpu"], 1)


def test_tokenized_match_does_not_broaden_results(claim_repository: ClaimRepository) -> None:
    """tokenized AND 查询命中完整文本时不应引入局部匹配。"""
    assert _traced_result_ids(claim_repository, "完整严格查询") == (["strict-exact"], 1)


def test_visibility_filter_does_not_issue_legacy_fallback(claim_repository: ClaimRepository) -> None:
    """v2 SQL 有 raw row 但后置可见性为空时，不应再发出 legacy fallback。"""
    assert _traced_result_ids(claim_repository, "不可见严格命中") == ([], 1)


def test_tokenized_fts_handles_quoted_mixed_query(claim_repository: ClaimRepository) -> None:
    """双引号应作为安全边界处理，统一 lexicalizer 仍可召回且不产生语法错误。"""
    assert _result_ids(claim_repository, '用户的"GPU"型号是什么') == ["gpu"]


def test_trigram_fallback_query_uses_boundary_aware_or_phrases() -> None:
    """Fallback 应按 CJK/Latin 边界生成去重且安全引用的 OR phrase。"""
    assert build_fts_trigram_fallback_query("用户的GPU型号是什么") == (
        '"用户的" OR "GPU" OR "型号是" OR "号是什" OR "是什么"'
    )


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("GPU gpu GPU", '"GPU"'),
        ("RTX5070 12", '"RTX5070"'),
        ("abc中文def", '"abc" OR "def"'),
        ("RTX-5070/Ti", '"RTX" OR "5070"'),
        ("哈哈哈哈", '"哈哈哈"'),
    ],
)
def test_trigram_fallback_query_filters_boundaries_and_duplicates(query: str, expected: str) -> None:
    """Fallback 应稳定去重，并过滤边界处分出的短片段。"""
    assert build_fts_trigram_fallback_query(query) == expected


def test_short_query_returns_empty(claim_repository: ClaimRepository) -> None:
    """没有对应词项的短查询应安全返回空结果。"""
    assert _result_ids(claim_repository, "FT") == []


@pytest.mark.parametrize("query", ["C++", "foo-bar"])
def test_special_characters_quoted(claim_repository: ClaimRepository, query: str) -> None:
    """FTS 特殊字符应被 phrase quoting 安全处理且不抛异常。"""
    assert _result_ids(claim_repository, query) == []


@pytest.mark.parametrize("query", ["", "   ", "，。！？", '"'])
def test_non_searchable_query_issues_no_match(
    claim_repository: ClaimRepository,
    query: str,
) -> None:
    """空白或纯标点查询应安全返回空结果，且不执行任何 MATCH。"""
    assert build_fts_trigram_fallback_query(query) == ""
    assert _traced_result_ids(claim_repository, query) == ([], 0)
