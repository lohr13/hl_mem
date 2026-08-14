"""基于私有小语料和真实检索组件的隔离中文召回评测。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from hl_mem.application.recall import RecallService
from hl_mem.components import make_embedder, make_reranker
from hl_mem.config_loader import load_settings
from hl_mem.storage.database import Database
from tests.eval.chinese_recall import (
    QueryEmbeddingCache,
    build_corpus,
    evaluate_cases,
    load_cases,
    load_corpus,
)
from tests.eval.real_chinese_data import (
    MEMDAILY_CASES_NAME,
    MEMDAILY_CORPUS_NAME,
    PERLTQA_CASES_NAME,
    PERLTQA_CORPUS_NAME,
)

pytestmark = [pytest.mark.eval, pytest.mark.real_api]

PRIVATE_DATASET_DIR = Path.home() / "hl_mem_eval_data" / "datasets"
RESULT_LIMIT = 5


@dataclass(frozen=True)
class SuiteSpec:
    corpus_path: Path
    cases_path: Path
    corpus_count: int
    case_count: int
    positive_case_count: int
    no_answer_case_count: int
    preference_case_count: int
    minimum_hit_at_1: float
    minimum_hit_at_5: float
    minimum_mrr: float
    minimum_answerability: float
    minimum_gold_recall: float
    minimum_complete_evidence: float
    minimum_no_answer: float
    minimum_slice_hit_at_1: float
    minimum_slice_hit_at_5: float
    minimum_preference_hit_at_1: float
    minimum_preference_hit_at_5: float


SUITES = {
    "breadth": SuiteSpec(
        PRIVATE_DATASET_DIR / PERLTQA_CORPUS_NAME,
        PRIVATE_DATASET_DIR / PERLTQA_CASES_NAME,
        895,
        64,
        56,
        8,
        12,
        0.82,
        0.95,
        0.89,
        0.68,
        0.95,
        0.95,
        0.25,
        0.65,
        0.85,
        0.75,
        0.90,
    ),
    "depth": SuiteSpec(
        PRIVATE_DATASET_DIR / MEMDAILY_CORPUS_NAME,
        PRIVATE_DATASET_DIR / MEMDAILY_CASES_NAME,
        308,
        48,
        42,
        6,
        8,
        0.93,
        0.95,
        0.95,
        0.55,
        0.85,
        0.75,
        0.33,
        0.70,
        0.85,
        0.80,
        0.90,
    ),
    "legacy-smoke": SuiteSpec(
        PRIVATE_DATASET_DIR / "chinese_recall_corpus.jsonl",
        PRIVATE_DATASET_DIR / "chinese_fts_eval.jsonl",
        12,
        12,
        9,
        3,
        3,
        0.90,
        0.80,
        0.90,
        1.0,
        0.80,
        0.80,
        1.0,
        0.0,
        0.0,
        0.90,
        0.80,
    ),
    "legacy-full": SuiteSpec(
        PRIVATE_DATASET_DIR / "chinese_recall_corpus.jsonl",
        PRIVATE_DATASET_DIR / "recall_v2.jsonl",
        12,
        24,
        15,
        9,
        8,
        0.90,
        0.80,
        0.90,
        1.0,
        0.80,
        0.80,
        1.0,
        0.0,
        0.80,
        0.90,
        0.80,
    ),
}


def _suite_spec(pytestconfig: pytest.Config) -> tuple[str, SuiteSpec]:
    suite = pytestconfig.getoption("--chinese-eval-suite")
    return suite, SUITES[suite]


def test_chinese_recall_evaluation(
    pytestconfig: pytest.Config,
    tmp_path: Path,
) -> None:
    """在临时 corpus 上校验中文召回、no-answer 与自动意图路由。"""
    suite, spec = _suite_spec(pytestconfig)
    if not spec.corpus_path.is_file():
        pytest.skip(f"private evaluation corpus does not exist: {spec.corpus_path}")
    if not spec.cases_path.is_file():
        pytest.skip(f"private evaluation dataset does not exist: {spec.cases_path}")

    corpus = load_corpus(spec.corpus_path)
    memory_ids = {claim.memory_id for claim in corpus}
    cases = load_cases(spec.cases_path, memory_ids)
    assert len(corpus) == spec.corpus_count, f"expected {spec.corpus_count} corpus claims, got {len(corpus)}"
    assert len(cases) == spec.case_count, f"expected {spec.case_count} {suite} cases, got {len(cases)}"
    assert sum(case.expected_type == "claim" for case in cases) == spec.positive_case_count
    assert sum(case.expected_type == "empty" for case in cases) == spec.no_answer_case_count
    assert (
        sum(case.expected_type == "claim" and case.expected_intent == "preference" for case in cases)
        >= spec.preference_case_count
    )
    if suite == "legacy-smoke":
        assert sum(case.expected_type == "claim" for case in cases) == 9
        assert sum(case.expected_type == "empty" for case in cases) == 3
    elif suite in {"breadth", "depth"}:
        assert all(case.intent_override is None for case in cases)

    configured = load_settings()
    assert configured.embedder_mode == "real", "Chinese evaluation requires embedding.mode='real'"
    assert configured.embedding_api_key, "Chinese evaluation requires EMBEDDING_API_KEY"
    assert configured.reranker_mode in {"on", "real"}, "Chinese evaluation requires a real reranker"
    assert configured.reranker_api_key, "Chinese evaluation requires RERANKER_API_KEY"
    runtime_settings = replace(
        configured,
        database_path=str(tmp_path / "chinese-recall-eval.db"),
        vector_backend="sqlite_scan",
        query_expansion_mode="off",
        relation_discovery_mode="off",
        procedure_recall_mode="off",
        relevance_gate_mode="enforce",
        relevance_intents=("current_state",),
        recall_side_effect_backoff_seconds=0.0,
    )
    runtime_settings.validate()

    real_embedder = make_embedder(runtime_settings)
    embedder = QueryEmbeddingCache(real_embedder, [case.query for case in cases])
    reranker = make_reranker(runtime_settings)
    assert reranker is not None
    database = Database(settings=runtime_settings)
    connection = database.open()
    try:
        build_corpus(connection, corpus, embedder, runtime_settings)
        report = evaluate_cases(
            RecallService(
                connection,
                embedder,
                reranker,
                settings=runtime_settings,
            ),
            cases,
            limit=RESULT_LIMIT,
        )
    finally:
        database.close()

    for case, item in zip(cases, report.items, strict=True):
        print(
            f"\n[{case.case_id}] query={case.query!r} expected={case.expected_type}"
            f"\n  returned={list(item.returned_ids)} rank={item.rank} answerability={item.answerability}"
            f"\n  matched_gold={list(item.matched_expected_ids)}/{item.expected_count}"
            f"\n  top_score={item.top_score} runner_up={item.runner_up_score}"
            f" reranker={item.top_reranker_score} dense={item.top_dense_score}"
            f" channels={list(item.top_channels)} relevance="
            f"{item.top_relevance_decision}/{item.top_relevance_reason}"
            f"\n  intent={item.actual_intent}/{item.intent_source} expected="
            f"{case.expected_intent}/{case.expected_intent_source}"
        )
    no_answer_display = (
        f"{report.no_answer_accuracy:.3f}" if any(case.expected_type == "empty" for case in cases) else "n/a"
    )
    print(
        "\nChinese isolated recall summary:"
        f"\n  suite={suite} cases={report.case_count}"
        f"\n  Hybrid Hit@1={report.hit_at_1:.3f}, Hit@5={report.hit_at_5:.3f}, MRR={report.mrr:.3f}"
        f"\n  Gold recall@5={report.mean_gold_recall:.3f}"
        f" complete-evidence={report.complete_evidence_accuracy:.3f}"
        f"\n  Positive answerability={report.positive_answerability_accuracy:.3f}"
        f"\n  No-answer accuracy={no_answer_display} P/R/F1="
        f"{report.no_answer_precision:.3f}/{report.no_answer_recall:.3f}/{report.no_answer_f1:.3f}"
        f"\n  Hard abstention P/R/F1={report.hard_abstention_precision:.3f}/"
        f"{report.hard_abstention_recall:.3f}/{report.hard_abstention_f1:.3f}"
        f"\n  Soft abstention P/R/F1={report.soft_abstention_precision:.3f}/"
        f"{report.soft_abstention_recall:.3f}/{report.soft_abstention_f1:.3f}"
        f"\n  Intent accuracy={report.intent_accuracy:.3f}"
    )

    positive_items = [item for item in report.items if item.correct_no_answer is None]
    preference_items = [
        item
        for case, item in zip(cases, report.items, strict=True)
        if case.expected_type == "claim" and case.expected_intent == "preference"
    ]
    preference_hit_at_1 = (
        sum(item.rank == 1 for item in preference_items) / len(preference_items) if preference_items else 1.0
    )
    preference_hit_at_5 = (
        sum(item.rank is not None and item.rank <= RESULT_LIMIT for item in preference_items) / len(preference_items)
        if preference_items
        else 1.0
    )
    slice_hit_at_5 = {
        slice_name: sum(
            item.rank is not None and item.rank <= RESULT_LIMIT for item in positive_items if item.slice == slice_name
        )
        / sum(item.slice == slice_name for item in positive_items)
        for slice_name in sorted({item.slice for item in positive_items})
    }
    slice_hit_at_1 = {
        slice_name: sum(item.rank == 1 for item in positive_items if item.slice == slice_name)
        / sum(item.slice == slice_name for item in positive_items)
        for slice_name in sorted({item.slice for item in positive_items})
    }
    print(f"  Preference Hit@1={preference_hit_at_1:.3f}, Hit@5={preference_hit_at_5:.3f}")
    print(f"  Slice Hit@1={slice_hit_at_1}")
    print(f"  Slice Hit@5={slice_hit_at_5}")

    assert (
        report.hit_at_1 >= spec.minimum_hit_at_1
    ), f"Hybrid Hit@1 regressed: {report.hit_at_1:.3f} < {spec.minimum_hit_at_1:.3f}"
    assert (
        report.hit_at_5 >= spec.minimum_hit_at_5
    ), f"Hybrid Hit@5 regressed: {report.hit_at_5:.3f} < {spec.minimum_hit_at_5:.3f}"
    assert report.mrr >= spec.minimum_mrr, f"MRR regressed: {report.mrr:.3f} < {spec.minimum_mrr:.3f}"
    assert report.positive_answerability_accuracy >= spec.minimum_answerability, (
        "positive answerability regressed: "
        f"{report.positive_answerability_accuracy:.3f} < {spec.minimum_answerability:.3f}"
    )
    assert (
        report.mean_gold_recall >= spec.minimum_gold_recall
    ), f"gold recall regressed: {report.mean_gold_recall:.3f} < {spec.minimum_gold_recall:.3f}"
    assert report.complete_evidence_accuracy >= spec.minimum_complete_evidence, (
        "complete-evidence accuracy regressed: "
        f"{report.complete_evidence_accuracy:.3f} < {spec.minimum_complete_evidence:.3f}"
    )
    if any(case.expected_type == "empty" for case in cases):
        assert (
            report.no_answer_accuracy >= spec.minimum_no_answer
        ), f"no-answer accuracy regressed: {report.no_answer_accuracy:.3f} < {spec.minimum_no_answer:.3f}"
    assert (
        preference_hit_at_5 >= spec.minimum_preference_hit_at_5
    ), f"preference Hit@5 regressed: {preference_hit_at_5:.3f} < {spec.minimum_preference_hit_at_5:.3f}"
    assert (
        preference_hit_at_1 >= spec.minimum_preference_hit_at_1
    ), f"preference Hit@1 regressed: {preference_hit_at_1:.3f} < {spec.minimum_preference_hit_at_1:.3f}"
    assert all(
        value >= spec.minimum_slice_hit_at_1 for value in slice_hit_at_1.values()
    ), f"slice Hit@1 regressed below {spec.minimum_slice_hit_at_1:.3f}: {slice_hit_at_1}"
    assert all(
        value >= spec.minimum_slice_hit_at_5 for value in slice_hit_at_5.values()
    ), f"slice Hit@5 regressed below {spec.minimum_slice_hit_at_5:.3f}: {slice_hit_at_5}"
    assert report.intent_accuracy == 1.0, "recall intent or intent_source did not match the case contract"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-m", "real_api", "-s"]))
