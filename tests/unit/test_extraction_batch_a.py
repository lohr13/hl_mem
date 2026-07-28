"""批次 A benchmark 诊断、gold 数据集与评估脚本测试。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

from hl_mem.ingest.extractors import ExtractedClaim

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_script(name: str) -> ModuleType:
    """按文件名加载 scripts 下的可执行模块。"""
    path = PROJECT_ROOT / "scripts" / f"{name}.py"
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
        for line in (PROJECT_ROOT / "scripts" / "gold_dataset.jsonl").read_text(encoding="utf-8").splitlines()
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
