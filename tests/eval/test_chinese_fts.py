"""中文全文检索离线评测。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hl_mem.components import make_embedder, make_reranker
from hl_mem.config_loader import load_settings
from hl_mem.domain.recall import RecallIntent
from hl_mem.recall.recall_pipeline import hybrid_claims
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database

pytestmark = pytest.mark.eval

DATABASE_PATH = Path("var/hl_mem.db")
DATASET_PATH = Path(__file__).parent / "datasets" / "chinese_fts_eval.jsonl"
RESULT_LIMIT = 10


def _load_cases() -> list[dict[str, Any]]:
    """读取中文 FTS JSONL 评测集。"""
    return [json.loads(line) for line in DATASET_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]


def _claim_text(claim: dict[str, Any]) -> str:
    """读取用于 gold_text 原文片段匹配的持久化索引文本。"""
    text = claim.get("index_text") or claim.get("text")
    return text if isinstance(text, str) else ""


def _first_relevant_rank(case: dict[str, Any], results: list[dict[str, Any]]) -> int | None:
    """返回第一个包含 gold_text 原文片段的结果排名。"""
    gold_text = case.get("gold_text")
    if not isinstance(gold_text, str):
        return None
    needle = gold_text.casefold()
    for rank, result in enumerate(results, start=1):
        if needle in _claim_text(result).casefold():
            return rank
    return None


def _result_details(results: list[dict[str, Any]]) -> list[str]:
    """生成紧凑的逐条命中详情。"""
    return [
        f"{result.get('id', '?')}:{result.get('subject_entity_id', '?')}:{_claim_text(result)[:80]}"
        for result in results
    ]


def test_chinese_fts_retrieval_evaluation() -> None:
    """按 gold_text 记录中文 FTS-only 与混合召回排名指标。"""
    if not DATABASE_PATH.is_file():
        pytest.skip(f"evaluation database does not exist: {DATABASE_PATH}")

    cases = _load_cases()
    case_ids = [case.get("case_id") for case in cases]
    positive_cases = [case for case in cases if case.get("expected_type") == "claim"]
    empty_cases = [case for case in cases if case.get("expected_type") == "empty"]
    assert len(cases) == 30, f"expected 30 evaluation cases, got {len(cases)}"
    assert len(set(case_ids)) == len(case_ids), "evaluation case_id values must be unique"
    assert len(positive_cases) == 17, f"expected 17 positive cases, got {len(positive_cases)}"
    assert len(empty_cases) == 13, f"expected 13 no-answer cases, got {len(empty_cases)}"
    assert all(isinstance(case.get("gold_text"), str) and case["gold_text"].strip() for case in positive_cases)
    assert all(case.get("gold_text") is None for case in empty_cases)

    settings = load_settings()
    assert settings.embedder_mode == "real", "30-case evaluation requires HL_MEM_EMBEDDER=real"
    assert settings.embedding_model == "text-embedding-v4"
    assert settings.reranker_mode in {"on", "real"}, "30-case evaluation requires a real reranker"
    assert settings.reranker_model == "gte-rerank-v2"
    embedder = make_embedder(settings)
    reranker = make_reranker(settings)
    assert reranker is not None
    query_blobs = embedder.embed_batch([case["query"] for case in cases])
    database = Database(DATABASE_PATH)
    connection = database.open()
    repo = ClaimRepository(connection, settings=settings)
    fts_ranks: list[int | None] = []
    hybrid_ranks: list[int | None] = []
    positive_count = 0
    empty_correct = 0
    empty_count = 0

    try:
        for case, query_blob in zip(cases, query_blobs, strict=True):
            fts_results = repo.search_claims_fts(
                case["query"],
                RESULT_LIMIT,
                intent=RecallIntent.CURRENT_STATE,
            )
            hybrid_results = hybrid_claims(
                repo,
                case["query"],
                query_blob,
                RESULT_LIMIT,
                None,
                reranker,
                intent=RecallIntent.CURRENT_STATE,
            )

            if case["expected_type"] == "empty":
                empty_count += 1
                empty_correct += int(not fts_results and not hybrid_results)
            else:
                positive_count += 1
                fts_ranks.append(_first_relevant_rank(case, fts_results))
                hybrid_ranks.append(_first_relevant_rank(case, hybrid_results))

            print(
                f"\n[{case['case_id']}] query={case['query']!r} expected={case['expected_type']}"
                f"\n  FTS-only ({len(fts_results)}): {_result_details(fts_results)}"
                f"\n  Hybrid   ({len(hybrid_results)}): {_result_details(hybrid_results)}"
            )
    finally:
        database.close()

    def metrics(ranks: list[int | None]) -> tuple[float, float, float]:
        if not ranks:
            return 0.0, 0.0, 0.0
        return (
            sum(rank == 1 for rank in ranks) / len(ranks),
            sum(rank is not None and rank <= 5 for rank in ranks) / len(ranks),
            sum(1 / rank for rank in ranks if rank is not None) / len(ranks),
        )

    fts_hit1, fts_hit5, fts_mrr = metrics(fts_ranks)
    hybrid_hit1, hybrid_hit5, hybrid_mrr = metrics(hybrid_ranks)
    empty_precision = empty_correct / empty_count if empty_count else 0.0
    print(
        "\nChinese FTS evaluation summary:"
        f"\n  FTS-only: Hit@1={fts_hit1:.3f}, Hit@5={fts_hit5:.3f}, MRR={fts_mrr:.3f}"
        f"\n  Hybrid:   Hit@1={hybrid_hit1:.3f}, Hit@5={hybrid_hit5:.3f}, MRR={hybrid_mrr:.3f}"
        f"\n  Empty precision: {empty_correct}/{empty_count} = {empty_precision:.3f}"
    )
    assert hybrid_hit5 >= 0.75, f"Hybrid Hit@5 regressed: {hybrid_hit5:.3f} < 0.750"


if __name__ == "__main__":
    test_chinese_fts_retrieval_evaluation()
