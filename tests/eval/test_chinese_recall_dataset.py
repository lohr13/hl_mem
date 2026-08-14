"""不访问网络的私有中文评测数据契约。"""

import hashlib
import json
from pathlib import Path

import pytest

from tests.eval.chinese_recall import load_cases, load_corpus
from tests.eval.real_chinese_data import (
    MANIFEST_NAME,
    MEMDAILY_CASES_NAME,
    MEMDAILY_CORPUS_NAME,
    PERLTQA_CASES_NAME,
    PERLTQA_CORPUS_NAME,
)

pytestmark = pytest.mark.eval

PRIVATE_DATASET_DIR = Path.home() / "hl_mem_eval_data" / "datasets"
LEGACY_CORPUS_PATH = PRIVATE_DATASET_DIR / "chinese_recall_corpus.jsonl"
LEGACY_SMOKE_PATH = PRIVATE_DATASET_DIR / "chinese_fts_eval.jsonl"
LEGACY_FULL_PATH = PRIVATE_DATASET_DIR / "recall_v2.jsonl"


@pytest.mark.parametrize(
    (
        "corpus_name",
        "cases_name",
        "case_count",
        "positive_count",
        "no_answer_count",
        "minimum_preference_count",
        "expected_slice_counts",
    ),
    [
        (
            PERLTQA_CORPUS_NAME,
            PERLTQA_CASES_NAME,
            64,
            56,
            8,
            12,
            {
                "perltqa_profile": 16,
                "perltqa_social_relationship": 16,
                "perltqa_events": 16,
                "perltqa_dialogues": 16,
            },
        ),
        (
            MEMDAILY_CORPUS_NAME,
            MEMDAILY_CASES_NAME,
            48,
            42,
            6,
            8,
            {
                "memdaily_simple": 8,
                "memdaily_conditional": 8,
                "memdaily_comparative": 8,
                "memdaily_aggregative": 8,
                "memdaily_post_processing": 8,
                "memdaily_noisy": 8,
            },
        ),
    ],
)
def test_real_chinese_datasets_are_bound_to_their_isolated_corpus(
    corpus_name: str,
    cases_name: str,
    case_count: int,
    positive_count: int,
    no_answer_count: int,
    minimum_preference_count: int,
    expected_slice_counts: dict[str, int],
) -> None:
    """在付费评测前发现真实数据 schema、ID binding 和样本分布错误。"""
    corpus_path = PRIVATE_DATASET_DIR / corpus_name
    cases_path = PRIVATE_DATASET_DIR / cases_name
    if not corpus_path.is_file() or not cases_path.is_file():
        pytest.skip("real Chinese evaluation assets are not installed")

    corpus = load_corpus(corpus_path)
    cases = load_cases(cases_path, {claim.memory_id for claim in corpus})

    assert len(cases) == case_count
    assert sum(case.expected_type == "claim" for case in cases) == positive_count
    assert sum(case.expected_type == "empty" for case in cases) == no_answer_count
    assert all(case.intent_override is None for case in cases)
    assert (
        sum(case.expected_type == "claim" and case.expected_intent == "preference" for case in cases)
        >= minimum_preference_count
    )
    assert {slice_name: sum(case.slice == slice_name for case in cases) for slice_name in expected_slice_counts} == (
        expected_slice_counts
    )
    assert all(case.namespace != "default" for case in cases)
    namespaces = {claim.namespace for claim in corpus}
    assert {case.namespace for case in cases}.issubset(namespaces)


def test_real_chinese_manifest_pins_upstream_source_hashes() -> None:
    manifest_path = PRIVATE_DATASET_DIR / MANIFEST_NAME
    if not manifest_path.is_file():
        pytest.skip("real Chinese evaluation manifest is not installed")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 2
    assert manifest["suites"]["breadth"]["case_count"] == 64
    assert manifest["suites"]["breadth"]["positive_preference_case_count"] >= 12
    assert manifest["suites"]["depth"]["case_count"] == 48
    assert manifest["suites"]["depth"]["positive_preference_case_count"] >= 8

    for source in manifest["sources"].values():
        path = Path(source["path"])
        if not path.is_file():
            pytest.skip(f"upstream dataset is not installed: {path}")
        with path.open("rb") as stream:
            assert hashlib.file_digest(stream, "sha256").hexdigest() == source["sha256"]


def test_legacy_chinese_datasets_remain_available_for_no_answer_regression() -> None:
    """旧虚构集继续承担确定性 no-answer 与显式 intent 回归。"""
    paths = (LEGACY_CORPUS_PATH, LEGACY_SMOKE_PATH, LEGACY_FULL_PATH)
    if not all(path.is_file() for path in paths):
        pytest.skip("legacy Chinese evaluation assets are not installed")

    corpus = load_corpus(LEGACY_CORPUS_PATH)
    memory_ids = {claim.memory_id for claim in corpus}
    smoke = load_cases(LEGACY_SMOKE_PATH, memory_ids)
    full = load_cases(LEGACY_FULL_PATH, memory_ids)

    assert len(corpus) == 12
    assert len(smoke) == 12
    assert len(full) == 24
    assert sum(case.expected_type == "claim" for case in smoke) == 9
    assert sum(case.expected_type == "empty" for case in smoke) == 3
    assert sum(case.expected_intent == "preference" for case in smoke) >= 3
    assert all(case.intent_override is None for case in smoke)
    assert sum(case.intent_override is not None for case in full) == 1
