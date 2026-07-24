"""中文全文检索离线评测。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hl_mem.domain.recall import RecallIntent
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.recall.recall_pipeline import hybrid_claims
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database

pytestmark = pytest.mark.eval

DATABASE_PATH = Path("var/hl_mem.db")
DATASET_PATH = Path(__file__).parent / "datasets" / "chinese_fts_eval.jsonl"
RESULT_LIMIT = 10


def _load_cases() -> list[dict[str, Any]]:
    """读取中文 FTS JSONL 评测集。"""
    return [
        json.loads(line)
        for line in DATASET_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _claim_text(claim: dict[str, Any]) -> str:
    """拼接可用于诊断和关键词匹配的 claim 字段。"""
    fields = (
        claim.get("subject_entity_id"),
        claim.get("predicate"),
        claim.get("value_json"),
        claim.get("text"),
    )
    return " ".join(str(field) for field in fields if field is not None)


def _is_relevant(case: dict[str, Any], results: list[dict[str, Any]]) -> bool:
    """判断非空用例是否命中相关 subject 或期望关键词。"""
    expected_keywords = [keyword.casefold() for keyword in case["expected_keywords"]]
    relevant_subjects = {subject.casefold() for subject in case["relevant_subjects"]}
    for result in results:
        subject = str(result.get("subject_entity_id", "")).casefold()
        text = _claim_text(result).casefold()
        if subject in relevant_subjects or any(keyword in text for keyword in expected_keywords):
            return True
    return False


def _result_details(results: list[dict[str, Any]]) -> list[str]:
    """生成紧凑的逐条命中详情。"""
    return [
        f"{result.get('id', '?')}:{result.get('subject_entity_id', '?')}:{_claim_text(result)[:80]}"
        for result in results
    ]


def test_chinese_fts_retrieval_evaluation() -> None:
    """记录中文 FTS-only、混合召回与空结果精度。"""
    if not DATABASE_PATH.is_file():
        pytest.skip(f"evaluation database does not exist: {DATABASE_PATH}")

    cases = _load_cases()
    database = Database(DATABASE_PATH)
    connection = database.open()
    repo = ClaimRepository(connection)
    embedder = FakeEmbedder(2048)
    fts_hits = 0
    hybrid_hits = 0
    positive_count = 0
    empty_correct = 0
    empty_count = 0

    try:
        for case in cases:
            fts_results = repo.search_claims_fts(
                case["query"],
                RESULT_LIMIT,
                intent=RecallIntent.CURRENT_STATE,
            )
            hybrid_results = hybrid_claims(
                repo,
                case["query"],
                embedder.embed_one(case["query"]),
                RESULT_LIMIT,
                None,
                intent=RecallIntent.CURRENT_STATE,
            )

            if case["expected_type"] == "empty":
                empty_count += 1
                empty_correct += int(not fts_results and not hybrid_results)
            else:
                positive_count += 1
                fts_hits += int(_is_relevant(case, fts_results))
                hybrid_hits += int(_is_relevant(case, hybrid_results))

            print(
                f"\n[{case['case_id']}] query={case['query']!r} expected={case['expected_type']}"
                f"\n  FTS-only ({len(fts_results)}): {_result_details(fts_results)}"
                f"\n  Hybrid   ({len(hybrid_results)}): {_result_details(hybrid_results)}"
            )
    finally:
        database.close()

    fts_recall = fts_hits / positive_count if positive_count else 0.0
    hybrid_recall = hybrid_hits / positive_count if positive_count else 0.0
    empty_precision = empty_correct / empty_count if empty_count else 0.0
    print(
        "\nChinese FTS evaluation summary:"
        f"\n  FTS-only recall: {fts_hits}/{positive_count} = {fts_recall:.3f}"
        f"\n  Hybrid recall:   {hybrid_hits}/{positive_count} = {hybrid_recall:.3f}"
        f"\n  Empty precision: {empty_correct}/{empty_count} = {empty_precision:.3f}"
    )
