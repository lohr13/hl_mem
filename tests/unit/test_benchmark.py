"""Benchmark suite 的离线契约测试。"""

from __future__ import annotations

import json
import math
from pathlib import Path

from hl_mem.evaluation.longmemeval import LongMemEvalAdapter
from hl_mem.evaluation.metrics import (
    bootstrap_ci,
    evidence_precision_recall,
    mrr,
    ndcg_at_k,
    recall_at_k,
    temporal_correctness,
)
from hl_mem.evaluation.models import GoldTemporal
from hl_mem.evaluation.reporting import generate_json_report, generate_markdown_summary
from hl_mem.evaluation.runner import BenchmarkRunner

FIXTURE = Path(__file__).parents[1] / "fixtures" / "longmemeval_small.json"


def test_adapter_normalizes_roles_stable_ids_and_missing_time() -> None:
    """角色映射、稳定 ID 和固定 epoch fallback 发生变化时应失败。"""
    cases = list(LongMemEvalAdapter.from_fixture(FIXTURE))
    first = cases[0]

    assert first.events[0]["id"] == "lme:fixture-preference:0:m1"
    assert first.events[0]["actor_type"] == "user"
    assert first.events[1]["actor_type"] == "assistant"
    assert first.events[1]["occurred_at"] == "2000-01-01T00:00:01+00:00"
    assert first.gold_evidence_event_ids == ("lme:fixture-preference:0:m1",)


def test_adapter_scopes_duplicate_message_ids_by_session(tmp_path: Path) -> None:
    """仅在 session 内唯一的 message_id 不得碰撞事件或幂等键。"""
    fixture = tmp_path / "duplicate-message-ids.json"
    fixture.write_text(
        json.dumps(
            [
                {
                    "question_id": "duplicate",
                    "haystack_sessions": [
                        {
                            "session_id": "first",
                            "messages": [{"message_id": "m1", "content": "one"}],
                        },
                        {
                            "session_id": "second",
                            "messages": [{"message_id": "m1", "content": "two"}],
                        },
                    ],
                    "question": "which",
                    "answer_message_ids": ["second:m1"],
                }
            ]
        ),
        encoding="utf-8",
    )

    case = next(iter(LongMemEvalAdapter.from_fixture(fixture)))
    assert [event["id"] for event in case.events] == [
        "lme:duplicate:first:m1",
        "lme:duplicate:second:m1",
    ]
    assert len({event["idempotency_key"] for event in case.events}) == 2
    assert case.gold_evidence_event_ids == ("lme:duplicate:second:m1",)


def test_adapter_synthesizes_update_and_expiry_checkpoints() -> None:
    """显式更新或有效期不再生成 lifecycle checkpoint 时应失败。"""
    update, expiry = list(LongMemEvalAdapter.from_fixture(FIXTURE))[1:]

    assert any(
        checkpoint.expected_hidden_event_ids == ("lme:fixture-update:0:old",)
        for checkpoint in update.lifecycle_checkpoints
    )
    assert any(
        checkpoint.worker_action == "expire_ttl"
        for checkpoint in expiry.lifecycle_checkpoints
    )


def test_metrics_use_unique_evidence_ids_and_hand_calculated_values() -> None:
    """同一 evidence 被多个 claim 引用而重复计分时应失败。"""
    results = [
        {"evidence": [{"event_id": "a"}, {"event_id": "a"}]},
        {"evidence": [{"event_id": "x"}]},
        {"evidence": [{"event_id": "b"}]},
    ]

    assert recall_at_k(results, {"a", "b"}, 1) == 0.5
    assert recall_at_k(results, {"a", "b"}, 10) == 1.0
    assert mrr(results, {"a", "b"}) == 1.0
    assert math.isclose(
        ndcg_at_k(results, {"a", "b"}, 3), (1.0 + 0.5) / (1.0 + 1 / math.log2(3))
    )
    assert evidence_precision_recall(["a", "a", "x"], ["a", "b"]) == {
        "precision": 0.5,
        "recall": 0.5,
        "f1": 0.5,
    }


def test_temporal_correctness_checks_valid_and_recorded_intervals() -> None:
    """valid-time 或 recorded-time 越界仍算正确时应失败。"""
    gold = (
        GoldTemporal(
            "a",
            "2025-01-01T00:00:00+00:00",
            "2025-02-01T00:00:00+00:00",
            "2025-01-01T00:00:00+00:00",
            "2025-02-01T00:00:00+00:00",
            "2025-01-05T00:00:00+00:00",
            "2025-02-05T00:00:00+00:00",
        ),
    )
    results = [
        {
            "evidence": [{"event_id": "a"}],
            "valid_from": "2025-01-10T00:00:00+00:00",
            "valid_to": "2025-01-20T00:00:00+00:00",
            "occurred_at": "2025-01-10T00:00:00+00:00",
            "recorded_from": "2025-01-12T00:00:00+00:00",
            "recorded_to": None,
        },
        {
            "evidence": [{"event_id": "a"}],
            "valid_from": "2025-03-01T00:00:00+00:00",
            "occurred_at": "2025-03-01T00:00:00+00:00",
            "recorded_from": "2025-03-01T00:00:00+00:00",
        },
    ]

    assert temporal_correctness(results, gold) == {
        "overall": 0.5,
        "valid_time": 0.5,
        "occurred_time": 0.5,
        "recorded_time": 0.5,
    }
    assert bootstrap_ci([1.0, 1.0, 1.0], seed=7) == (1.0, 1.0)


def test_temporal_correctness_marks_missing_recorded_gold_not_applicable() -> None:
    """缺少 recorded-time gold 时不得拿 occurred-time 边界代替。"""
    gold = (GoldTemporal("a", "2025-01-01", None, "2025-01-01", None),)
    result = temporal_correctness(
        [
            {
                "evidence": [{"event_id": "a"}],
                "valid_from": "2025-01-02",
                "occurred_at": "2025-01-02",
                "recorded_from": "2099-01-01",
            }
        ],
        gold,
    )
    assert result["recorded_time"] == "not_applicable"
    assert result["overall"] == 1.0


def test_runner_config_hash_is_stable_and_databases_are_isolated(
    tmp_path: Path, monkeypatch: object
) -> None:
    """case 复用数据库或读取生产 DB、hash 含运行时间时应失败。"""
    production = tmp_path / "production.sqlite3"
    production.write_text("must not be opened", encoding="utf-8")
    monkeypatch.setenv("HL_MEM_DB_PATH", str(production))  # type: ignore[attr-defined]
    runner = BenchmarkRunner(limit=2)

    first = runner.run(
        FIXTURE, "all", ("extraction",), tmp_path / "first", keep_db=True
    )
    second = runner.run(
        FIXTURE, "all", ("extraction",), tmp_path / "second", keep_db=True
    )

    assert first["config_hash"] == second["config_hash"]
    assert len(list((tmp_path / "first" / "databases").glob("*.sqlite3"))) == 2
    assert production.read_text(encoding="utf-8") == "must not be opened"
    assert all(case["event_count"] <= 2 for case in first["cases"])


def test_reporting_markdown_is_rendered_from_json_values(tmp_path: Path) -> None:
    """Markdown 重新计算指标或遗漏失败 case 时应失败。"""
    result = {
        "schema_version": 1,
        "benchmark": "longmemeval",
        "subset": "core",
        "config_hash": "abc",
        "run": {"models": {}},
        "metrics": {"retrieval": {"recall_at_5": 0.75}},
        "categories": {"preference": {"retrieval": {"recall_at_5": 0.5}}},
        "cases": [{"case_id": "broken", "metrics": {}, "errors": ["boom"]}],
    }

    report_path = generate_json_report(result, tmp_path)
    summary_path = generate_markdown_summary(
        json.loads(report_path.read_text(encoding="utf-8")), tmp_path
    )

    assert json.loads(report_path.read_text(encoding="utf-8"))["schema_version"] == 1
    markdown = summary_path.read_text(encoding="utf-8")
    assert "0.7500" in markdown
    assert "broken" in markdown
