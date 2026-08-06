"""批次 A benchmark 诊断、gold 数据集与评估脚本测试。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from hl_mem.ingest.extractors import ExtractedClaim

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_script(name: str) -> ModuleType:
    """按文件名加载 evaluation/tools 下的可执行模块。"""
    path = PROJECT_ROOT / "evaluation" / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_benchmark_records_complete_claim_and_schema_paths() -> None:
    benchmark = _load_script("benchmark_extraction")
    claim = ExtractedClaim(
        subject="用户",
        predicate="偏好",
        value="深色模式",
        scope="permanent",
        importance=0.8,
        canonical_attribute="preference.ui_theme",
        canonical_slot="preference.ui_theme",
        topic_tags=["preference"],
        confidence=0.95,
        volatility="stable",
        qualifiers={"source": "explicit"},
        reason="用户明确陈述",
    )
    extractor = SimpleNamespace(
        extract=lambda _content, _context: [claim],
        last_input_tokens=10,
        last_output_tokens=5,
        last_usage_tokens=15,
        _last_schema_errors=[{"loc": ("claims", 0, "scope"), "type": "literal_error", "input": "forever"}],
    )

    result = benchmark.run_single_extraction(extractor, "我喜欢深色模式", {})

    assert result["claims_data"] == [
        {
            "subject": "用户",
            "predicate": "偏好",
            "value": "深色模式",
            "scope": "permanent",
            "importance": 0.8,
            "canonical_attribute": "preference.ui_theme",
            "canonical_slot": "preference.ui_theme",
            "topic_tags": ["preference"],
            "confidence": 0.95,
            "volatility": "stable",
            "qualifiers": {"source": "explicit"},
            "reason": "用户明确陈述",
        }
    ]
    assert result["schema_error_paths"] == ["claims.0.scope"]


def test_http_response_body_is_redacted_and_truncated() -> None:
    benchmark = _load_script("benchmark_extraction")
    body = 'api_key="sk-secret123456" Authorization: Bearer token-value ' + "x" * 600

    sanitized = benchmark.sanitize_http_response_body(body)

    assert "secret123456" not in sanitized
    assert "token-value" not in sanitized
    assert len(sanitized) == 500


def test_gold_dataset_has_20_records_and_all_categories() -> None:
    records = [
        json.loads(line)
        for line in (PROJECT_ROOT / "evaluation" / "datasets" / "gold_dataset.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]

    assert len(records) == 20
    assert {record["category"] for record in records} == {
        "user_pref",
        "project_config",
        "tool_workflow",
        "status_report",
        "chat_confirm",
        "long_content",
    }
    assert all(record["gold_claims"] or not record["should_memorize"] for record in records)


def test_gold_evaluator_matches_semantically_equivalent_values() -> None:
    evaluator = _load_script("eval_against_gold")
    gold = [
        {
            "subject": "hl_mem",
            "predicate": "配置",
            "value": "NO_PROXY 包含 aliyuncs.com",
            "scope": "permanent",
        }
    ]
    predicted = [
        {
            "subject": "hl_mem",
            "predicate": "配置",
            "value": "aliyuncs.com 已加入 NO_PROXY",
            "scope": "permanent",
        }
    ]

    matches = evaluator.match_claims(gold, predicted, value_threshold=0.5)

    assert len(matches) == 1


@pytest.mark.parametrize(
    ("gold_subject", "predicted_subject"),
    [
        ("USER", "用户"),
        ("ＨＬ－ＭＥＭ", "HL_MEM 项目"),
    ],
)
def test_gold_evaluator_normalizes_subjects_with_production_aliases(
    gold_subject: str,
    predicted_subject: str,
) -> None:
    evaluator = _load_script("eval_against_gold")
    gold = [{"subject": gold_subject, "predicate": "事实", "value": "已启用"}]
    predicted = [{"subject": predicted_subject, "predicate": "事实", "value": "已启用"}]

    matches = evaluator.match_claims(gold, predicted, value_threshold=0.62)

    assert len(matches) == 1


def test_gold_evaluator_matches_predicate_by_canonical_attribute() -> None:
    evaluator = _load_script("eval_against_gold")
    gold = [{"subject": "Hermes", "predicate": "配置", "value": "provider 为 hl_mem"}]
    predicted = [
        {
            "subject": "hermes",
            "predicate": "uses",
            "canonical_attribute": "config.provider",
            "value": "provider 为 hl_mem",
        }
    ]

    matches = evaluator.match_claims(gold, predicted, value_threshold=0.62)

    assert len(matches) == 1


def test_gold_evaluator_matches_legacy_fact_and_state_labels_by_canonical_family() -> None:
    evaluator = _load_script("eval_against_gold")
    gold = [{"subject": "hl_mem", "predicate": "状态", "value": "尚未实现 entity graph"}]
    predicted = [
        {
            "subject": "hl_mem",
            "predicate": "事实",
            "canonical_attribute": "fact.architecture",
            "value": "未实现实体关系图谱（entity graph）",
        }
    ]

    matches = evaluator.match_claims(gold, predicted, value_threshold=0.62)

    assert len(matches) == 1


@pytest.mark.parametrize(
    "predicted",
    [
        {"subject": "hl_mem", "predicate": "uses", "value": "provider 为 hl_mem"},
        {
            "subject": "hl_mem",
            "predicate": "uses",
            "canonical_attribute": "config.unregistered",
            "value": "provider 为 hl_mem",
        },
        {
            "subject": "hl_mem",
            "predicate": "事实",
            "canonical_attribute": "fact.other",
            "value": "provider 为 hl_mem",
        },
    ],
)
def test_gold_evaluator_does_not_bridge_predicates_without_a_compatible_registered_family(
    predicted: dict[str, str],
) -> None:
    evaluator = _load_script("eval_against_gold")
    gold = [{"subject": "hl_mem", "predicate": "配置", "value": "provider 为 hl_mem"}]

    matches = evaluator.match_claims(gold, [predicted], value_threshold=0.62)

    assert matches == []


def test_gold_evaluator_normalizes_value_formatting_before_scoring() -> None:
    evaluator = _load_script("eval_against_gold")

    url_score = evaluator.value_similarity(
        '"访问 https://EXAMPLE.com/api/，计数 1,000.0 次"',
        "访问 https://example.com/api，计数 1000 次",
    )
    number_score = evaluator.value_similarity("配置端口为 1.0", "配置端口为 1")

    assert url_score == pytest.approx(1.0)
    assert number_score == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("版本为 1.2", "版本为 12"),
        ("阈值为 -0.5", "阈值为 0.5"),
    ],
)
def test_gold_evaluator_rejects_conflicting_numeric_values(left: str, right: str) -> None:
    evaluator = _load_script("eval_against_gold")

    score = evaluator.value_similarity(left, right)

    assert score < 0.62


def test_gold_evaluator_gives_partial_values_an_intermediate_score() -> None:
    evaluator = _load_script("eval_against_gold")

    score = evaluator.value_similarity(
        "NO_PROXY 包含 aliyuncs.com 和 bigmodel.cn",
        "NO_PROXY 包含 aliyuncs.com",
    )

    assert 0.62 <= score < 1.0
