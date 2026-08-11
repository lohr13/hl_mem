"""Tests for deterministic FTS lexicalization."""

from hl_mem.recall.lexicalizer import prepare_fts_document, prepare_fts_query, tokenize_for_fts


def test_tokenize_for_fts_segments_chinese_and_filters_stopwords() -> None:
    tokens = tokenize_for_fts("提取模型是什么")

    assert "提取" in tokens
    assert "模型" in tokens
    assert "是" not in tokens
    assert "什么" not in tokens


def test_tokenize_for_fts_keeps_short_domain_term_whole() -> None:
    assert tokenize_for_fts("姓名") == ("姓名",)


def test_tokenize_for_fts_handles_mixed_technical_and_chinese_text() -> None:
    assert tokenize_for_fts("GPU型号") == ("GPU", "型号")


def test_tokenize_for_fts_keeps_identifier_and_adds_its_segments() -> None:
    assert tokenize_for_fts("text-embedding-v4") == (
        "text-embedding-v4",
        "text",
        "embedding",
        "embed",
        "v4",
    )


def test_tokenize_for_fts_filters_all_configured_stopwords() -> None:
    assert tokenize_for_fts("的 是 什么 哪个") == ()


def test_prepare_fts_document_normalizes_and_deduplicates_stably() -> None:
    assert prepare_fts_document("ＧＰＵ型号 GPU") == "GPU 型号"


def test_prepare_fts_query_quotes_terms_and_neutralizes_operators() -> None:
    assert prepare_fts_query('GPU "型号" OR *') == '"GPU" AND "型号"'


def test_prepare_fts_query_groups_auto_raw_and_stem_variants() -> None:
    assert prepare_fts_query("running databases", language="auto") == (
        '("running" AND "databases") OR ("run" AND "databas")'
    )


def test_prepare_fts_functions_return_empty_string_for_empty_query() -> None:
    assert prepare_fts_document(" \t\n") == ""
    assert prepare_fts_query("") == ""
