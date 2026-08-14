"""隔离中文召回评测的数据契约与生产路由测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hl_mem.application.recall import RecallService
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.settings import Settings
from hl_mem.storage.database import Database
from tests.eval.chinese_recall import (
    DatasetError,
    QueryEmbeddingCache,
    build_corpus,
    evaluate_cases,
    load_cases,
    load_corpus,
)


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _corpus_row(memory_id: str = "editor") -> dict[str, object]:
    return {
        "memory_id": memory_id,
        "subject": "用户",
        "predicate": "偏好",
        "value": "用户偏好使用星河编辑器编写 Python",
        "canonical_attribute": "preference.tool_choice",
        "canonical_slot": "preference.tool_choice",
        "qualifiers": {"task": "编写 Python"},
        "topic_tags": ["tool_choice", "preference"],
        "importance": 0.9,
    }


def _case_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "case_id": "recommend-editor",
        "query": "你能给我推荐一个编写 Python 的编辑器吗？",
        "expected_type": "claim",
        "expected_memory_ids": ["editor"],
        "expected_intent": "preference",
        "expected_intent_source": "keyword",
        "slice": "preference_route",
    }
    row.update(overrides)
    return row


def test_load_private_corpus_and_cases_binds_only_stable_memory_ids(tmp_path: Path) -> None:
    corpus_path = tmp_path / "corpus.jsonl"
    cases_path = tmp_path / "cases.jsonl"
    _write_jsonl(corpus_path, [_corpus_row()])
    _write_jsonl(cases_path, [_case_row()])

    corpus = load_corpus(corpus_path)
    cases = load_cases(cases_path, {claim.memory_id for claim in corpus})

    assert corpus[0].memory_id == "editor"
    assert corpus[0].topic_tags == ("tool_choice", "preference")
    assert cases[0].expected_memory_ids == ("editor",)
    assert cases[0].intent_override is None


def test_evaluation_preserves_dataset_namespace_isolation(tmp_path: Path) -> None:
    corpus_path = tmp_path / "corpus.jsonl"
    cases_path = tmp_path / "cases.jsonl"
    _write_jsonl(corpus_path, [{**_corpus_row(), "namespace": "persona:lin-qing"}])
    _write_jsonl(cases_path, [{**_case_row(), "namespace": "persona:lin-qing"}])
    corpus = load_corpus(corpus_path)
    cases = load_cases(cases_path, {"editor"})
    settings = Settings.for_test()
    database = Database(tmp_path / "namespace-eval.db")
    connection = database.open()
    embedder = FakeEmbedder(settings.embedding_dim)

    class NamespaceCapturingService:
        namespaces: list[str] = []

        def recall(self, *_args: object, **kwargs: object) -> dict[str, object]:
            self.namespaces.append(str(kwargs.get("namespace")))
            return {
                "results": [{"id": "editor", "text": "命中", "score": 1.0}],
                "answerability": "supported",
                "search_trace": {"intent": "preference", "intent_source": "keyword"},
            }

    service = NamespaceCapturingService()
    try:
        build_corpus(connection, corpus, embedder, settings)
        stored_namespace = connection.execute("SELECT namespace_key FROM claims WHERE id = 'editor'").fetchone()[0]
        evaluate_cases(service, cases, limit=5)
    finally:
        database.close()

    assert corpus[0].namespace == "persona:lin-qing"
    assert cases[0].namespace == "persona:lin-qing"
    assert stored_namespace == "persona:lin-qing"
    assert service.namespaces == ["persona:lin-qing"]


def test_load_cases_rejects_dangling_memory_id(tmp_path: Path) -> None:
    cases_path = tmp_path / "cases.jsonl"
    _write_jsonl(cases_path, [_case_row(expected_memory_ids=["missing"])])

    with pytest.raises(DatasetError, match="missing"):
        load_cases(cases_path, {"editor"})


def test_load_corpus_rejects_non_operational_canonical_slot(tmp_path: Path) -> None:
    corpus_path = tmp_path / "corpus.jsonl"
    _write_jsonl(
        corpus_path,
        [
            {
                **_corpus_row(),
                "canonical_attribute": "preference.architecture",
                "canonical_slot": "preference.architecture",
                "qualifiers": {},
            }
        ],
    )

    with pytest.raises(DatasetError, match="canonical_slot"):
        load_corpus(corpus_path)


def test_load_corpus_rejects_unregistered_topic_tag(tmp_path: Path) -> None:
    corpus_path = tmp_path / "corpus.jsonl"
    _write_jsonl(corpus_path, [{**_corpus_row(), "topic_tags": ["preference", "programming"]}])

    with pytest.raises(DatasetError, match="topic_tags"):
        load_corpus(corpus_path)


def test_load_cases_rejects_gold_on_no_answer_case(tmp_path: Path) -> None:
    cases_path = tmp_path / "cases.jsonl"
    _write_jsonl(
        cases_path,
        [
            _case_row(
                expected_type="empty",
                expected_memory_ids=["editor"],
                expected_intent="current_state",
                slice="no_answer",
            )
        ],
    )

    with pytest.raises(DatasetError, match="empty"):
        load_cases(cases_path, {"editor"})


def test_isolated_corpus_exercises_automatic_preference_routing(tmp_path: Path) -> None:
    corpus_path = tmp_path / "corpus.jsonl"
    cases_path = tmp_path / "cases.jsonl"
    _write_jsonl(corpus_path, [_corpus_row()])
    _write_jsonl(cases_path, [_case_row()])
    corpus = load_corpus(corpus_path)
    cases = load_cases(cases_path, {"editor"})
    settings = Settings.for_test()
    database = Database(tmp_path / "eval.db")
    connection = database.open()
    embedder = FakeEmbedder(settings.embedding_dim)

    try:
        build_corpus(connection, corpus, embedder, settings)
        report = evaluate_cases(
            RecallService(connection, embedder, settings=settings),
            cases,
            limit=5,
        )
    finally:
        database.close()

    assert report.case_count == 1
    assert report.intent_accuracy == 1.0
    assert report.items[0].actual_intent == "preference"
    assert report.items[0].intent_source == "keyword"
    assert report.items[0].rank == 1


def test_no_answer_scores_low_confidence_as_abstention(tmp_path: Path) -> None:
    cases_path = tmp_path / "cases.jsonl"
    _write_jsonl(
        cases_path,
        [
            _case_row(
                case_id="unknown",
                query="用户的航海执照编号是什么？",
                expected_type="empty",
                expected_memory_ids=[],
                expected_intent="current_state",
                slice="no_answer",
            )
        ],
    )
    cases = load_cases(cases_path, {"editor"})

    class AbstainingService:
        def recall(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            return {
                "results": [{"id": "editor", "text": "不相关候选"}],
                "answerability": "low_confidence",
                "search_trace": {"intent": "current_state", "intent_source": "keyword"},
            }

    report = evaluate_cases(AbstainingService(), cases, limit=5)

    assert report.no_answer_accuracy == 1.0
    assert report.items[0].correct_no_answer is True


def test_positive_low_confidence_is_counted_as_answerability_failure(tmp_path: Path) -> None:
    cases_path = tmp_path / "cases.jsonl"
    _write_jsonl(cases_path, [_case_row()])
    cases = load_cases(cases_path, {"editor"})

    class LowConfidenceService:
        def recall(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            return {
                "results": [{"id": "editor", "text": "命中但拒答"}],
                "answerability": "low_confidence",
                "search_trace": {"intent": "preference", "intent_source": "keyword"},
            }

    report = evaluate_cases(LowConfidenceService(), cases, limit=5)

    assert report.hit_at_1 == 1.0
    assert report.positive_answerability_accuracy == 0.0
    assert report.items[0].correct_positive_answer is False


def test_missing_answerability_cannot_pass_positive_contract(tmp_path: Path) -> None:
    cases_path = tmp_path / "cases.jsonl"
    _write_jsonl(cases_path, [_case_row()])
    cases = load_cases(cases_path, {"editor"})

    class IncompleteService:
        def recall(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            return {
                "results": [{"id": "editor", "text": "只有命中，没有 answerability"}],
                "search_trace": {"intent": "preference", "intent_source": "keyword"},
            }

    report = evaluate_cases(IncompleteService(), cases, limit=5)

    assert report.items[0].answerability == "unknown"
    assert report.positive_answerability_accuracy == 0.0


def test_query_embedding_cache_batches_unique_queries_once() -> None:
    class CountingEmbedder(FakeEmbedder):
        def __init__(self) -> None:
            super().__init__(8)
            self.query_batches: list[list[str]] = []

        def embed_query_batch(self, texts: list[str]) -> list[bytes]:
            self.query_batches.append(list(texts))
            return self.embed_batch(texts)

    delegate = CountingEmbedder()
    cached = QueryEmbeddingCache(delegate, ["名字", "推荐工具", "名字"])

    assert cached.embed_query("名字") == delegate.embed_one("名字")
    assert cached.embed_query("推荐工具") == delegate.embed_one("推荐工具")
    assert delegate.query_batches == [["名字", "推荐工具"]]
