"""C-series live runner 的 planner/reader/recall 重试状态机。"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from evaluation.tools import run_c_series_relation_experiment as runner
from hl_mem.evaluation.c_series import IntentDecision, SufficiencyDecision
from hl_mem.evaluation.c_series_runtime import RecallExecution


class Retry429(RuntimeError):
    status_code = 429


def _retryable(kind: str) -> BaseException:
    return httpx.ReadTimeout("planner timeout") if kind == "timeout" else Retry429("planner rate limited")


def _execution() -> RecallExecution:
    seed = {
        "claim_id": "seed",
        "text": "项目负责人是赵岚",
        "entities": ["项目", "赵岚"],
        "slot": "fact",
        "token_count": 8,
        "evidence_provenance": [{"event_id": "e1"}],
    }
    return RecallExecution(
        packet=(dict(seed),),
        seed_packet=(dict(seed),),
        answerability="low_confidence",
        search_trace={"candidates": {}},
        recall_latency_seconds=0.01,
        relation_paths=(),
    )


def _case() -> dict:
    return {
        "case_id": "retry-case",
        "dataset": "relation_design_dev",
        "category": "cross_event_two_hop",
        "question": "项目负责人的常驻城市是哪里？",
        "namespace": "retry",
        "db_path": "unused.db",
    }


@pytest.mark.parametrize("kind", ["timeout", "429"])
async def test_planner_retryable_third_fallback_reports_full_task_latency(monkeypatch, kind: str) -> None:
    recall_calls: list[tuple[str, str]] = []
    planner_calls = 0
    reader_calls = 0
    clock = 0.0

    def fake_perf_counter() -> float:
        return clock

    def fake_recall(case, settings, embedder, reranker, *, db_path, arm_id):
        nonlocal clock
        del settings, embedder, reranker, db_path
        recall_calls.append((case["case_id"], arm_id))
        if len(recall_calls) > 1:
            clock += 0.5  # retry scheduling gap
        clock += 1.0
        return _execution()

    async def fake_chat(client, settings, *, system, user, max_tokens, timeout):
        nonlocal clock, planner_calls, reader_calls
        del client, settings, user, max_tokens, timeout
        if system == runner.PLANNER_SYSTEM:
            planner_calls += 1
            clock += 2.0
            raise _retryable(kind)
        reader_calls += 1
        clock += 3.0
        return "信息不足", {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}

    monkeypatch.setattr(runner.time, "perf_counter", fake_perf_counter)
    monkeypatch.setattr(runner, "recall_visible_case", fake_recall)
    monkeypatch.setattr(runner, "_chat", fake_chat)
    monkeypatch.setattr(
        runner,
        "_sufficiency",
        lambda case, execution: (
            IntentDecision(True, ("two_hop",), ("role", "action", "object")),
            SufficiencyDecision(0.5, 0.0, 0.0, 0.2, True, ("weighted_score",)),
        ),
    )

    result = await runner._run_with_retry(
        object(),
        runner.asyncio.Semaphore(1),
        _case(),
        2,
        "f4",
        SimpleNamespace(llm_timeout=30.0),
        object(),
        None,
    )

    assert result["status"] == "complete"
    assert (result["case_id"], result["repeat_index"], result["arm_id"]) == ("retry-case", 2, "f4")
    assert result["attempts"] == 3
    assert result["planner_attempts"] == 3
    assert result["planner_error"] in {"ReadTimeout", "Retry429"}
    assert result["retry_errors"] == [result["planner_error"], result["planner_error"]]
    assert result["e2e_latency_seconds"] == pytest.approx(13.0)
    assert [item["claim_id"] for item in result["packet"]] == ["seed"]
    assert recall_calls == [("retry-case", "f4")] * 3
    assert planner_calls == 3
    assert reader_calls == 1


@pytest.mark.parametrize("stage", ["reader", "recall"])
async def test_non_planner_retryable_still_raises_after_third_attempt(monkeypatch, stage: str) -> None:
    recall_calls = 0
    reader_calls = 0

    def fake_recall(case, settings, embedder, reranker, *, db_path, arm_id):
        nonlocal recall_calls
        del case, settings, embedder, reranker, db_path, arm_id
        recall_calls += 1
        if stage == "recall":
            raise httpx.ReadTimeout("recall timeout")
        return _execution()

    async def fake_chat(client, settings, *, system, user, max_tokens, timeout):
        nonlocal reader_calls
        del client, settings, system, user, max_tokens, timeout
        reader_calls += 1
        raise httpx.ReadTimeout("reader timeout")

    monkeypatch.setattr(runner, "recall_visible_case", fake_recall)
    monkeypatch.setattr(runner, "_chat", fake_chat)
    monkeypatch.setattr(
        runner,
        "_sufficiency",
        lambda case, execution: (
            IntentDecision(False, (), ()),
            SufficiencyDecision(0.5, None, None, 0.5, True, ("weighted_score",)),
        ),
    )

    with pytest.raises(httpx.ReadTimeout) as raised:
        await runner._run_with_retry(
            object(),
            runner.asyncio.Semaphore(1),
            _case(),
            2,
            "C0",
            SimpleNamespace(llm_timeout=30.0),
            object(),
            None,
        )

    assert recall_calls == 3
    assert reader_calls == (3 if stage == "reader" else 0)
    assert runner.is_retryable_error(raised.value) is True
