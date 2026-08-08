from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

import httpx

from evaluation.tools import merge_longmemeval_results as merger
from evaluation.tools import run_longmemeval_benchmark as runner
from hl_mem.http_utils import retry_http
from hl_mem.settings import Settings


def _record(case_id: str) -> dict[str, object]:
    return {
        "question_id": case_id,
        "question_type": "multi-session",
        "question": f"Question for {case_id}?",
        "answer": f"Answer for {case_id}",
        "question_date": "2023/05/30 (Tue) 23:40",
        "answer_session_ids": [f"session-{case_id}"],
        "haystack_dates": ["2023/05/20 (Sat) 02:21"],
        "haystack_session_ids": [f"session-{case_id}"],
        "haystack_sessions": [[{"role": "user", "content": f"Memory for {case_id}"}]],
    }


def _case_result(
    case_id: str,
    *,
    correct: bool = True,
    error: str | None = None,
    qa_evaluated: bool = True,
) -> dict[str, object]:
    return {
        "case_id": case_id,
        "question_type": "multi-session",
        "retrieval": {"eligible": False},
        "qa": {"correct": correct} if qa_evaluated else None,
        "error": error,
    }


def _shard_report(
    dataset_sha256: str,
    cases: list[dict[str, object]],
    *,
    qa_enabled: bool = True,
    run_limit: int | None = None,
    reader_context_mode: str = "windowed",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "benchmark": "LongMemEval-S",
        "status": "completed",
        "dataset": {"sha256": dataset_sha256},
        "run": {
            "started_at": "2026-08-08T00:00:00+00:00",
            "package_version": "v0.24.0",
            "limit": run_limit,
            "offset": 0,
            "qa_enabled": qa_enabled,
            "reader_context_mode": reader_context_mode,
            "models": {
                "extractor": "qwen3.7-plus",
                "extractor_provider": "dashscope",
                "extractor_version": runner.LLM_EXTRACTOR_VERSION,
                "embedder": "qwen3.7-text-embedding",
                "embedding_dim": 2048,
                "embedding_api_mode": "native",
                "embedding_text_type": None,
                "reranker": "off",
                "reader": "qwen3.7-plus" if qa_enabled else "not_run",
                "judge": "qwen3.7-plus" if qa_enabled else "not_run",
            },
        },
        "cases": cases,
    }


class LongMemEvalBatchRunnerTests(unittest.TestCase):
    def test_reader_context_mode_defaults_to_windowed_and_accepts_head(self) -> None:
        self.assertEqual(runner.parse_args([]).reader_context_mode, "windowed")
        self.assertEqual(runner.parse_args(["--reader-context-mode", "head"]).reader_context_mode, "head")

    def test_offset_applies_before_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dataset = Path(directory) / "dataset.json"
            dataset.write_text(json.dumps([_record(f"case-{index}") for index in range(5)]), encoding="utf-8")

            selected = list(runner.iter_case_records(dataset, offset=2, limit=2))

        self.assertEqual([record["question_id"] for record in selected], ["case-2", "case-3"])

    def test_resume_skips_and_preserves_completed_case(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "dataset.json"
            output = root / "result.json"
            dataset.write_text(json.dumps([_record("case-1"), _record("case-2")]), encoding="utf-8")
            dataset_sha256 = hashlib.sha256(dataset.read_bytes()).hexdigest()
            output.write_text(
                json.dumps(
                    _shard_report(
                        dataset_sha256,
                        [_case_result("case-1", qa_evaluated=False)],
                        qa_enabled=False,
                    )
                ),
                encoding="utf-8",
            )
            settings = Settings(
                embedding_model="qwen3.7-text-embedding",
                embedding_dim=2048,
                embedding_api_mode="native",
                embedding_text_type=None,
            )

            def run_case(case: runner.LongMemEvalCase, *_args: object, **_kwargs: object) -> dict[str, object]:
                return _case_result(case.case_id, qa_evaluated=False)

            with (
                patch.object(runner, "load_settings", return_value=settings),
                patch.object(runner, "_validate_production_settings"),
                patch.object(runner, "initialize_process"),
                patch.object(runner, "make_embedder", return_value=object()),
                patch.object(runner, "make_reranker", return_value=None),
                patch.object(runner, "_run_case", side_effect=run_case) as run,
            ):
                exit_code = runner.main(
                    [
                        "--dataset",
                        str(dataset),
                        "--output",
                        str(output),
                        "--resume",
                        "--no-qa",
                    ]
                )

            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0].case_id, "case-2")
        self.assertEqual(run.call_args.kwargs["reader_context_mode"], "windowed")
        self.assertEqual(report["run"]["reader_context_mode"], "windowed")
        self.assertEqual([case["case_id"] for case in report["cases"]], ["case-1", "case-2"])

    def test_qa_retry_recovers_from_429(self) -> None:
        request = httpx.Request("POST", "https://example.test/chat/completions")
        response = httpx.Response(429, request=request)
        attempts = 0

        def call_qa() -> str:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise httpx.HTTPStatusError("rate limited", request=request, response=response)
            return "success"

        with patch.object(runner.time, "sleep") as sleep:
            result = runner._qa_call_with_retry(call_qa)

        self.assertEqual(result, "success")
        self.assertEqual(attempts, 3)
        self.assertEqual(sleep.call_args_list, [call(2.0), call(4.0)])

    def test_qa_retry_honors_retry_after(self) -> None:
        request = httpx.Request("POST", "https://example.test/chat/completions")
        response = httpx.Response(429, request=request, headers={"Retry-After": "7"})
        attempts = 0

        def call_qa() -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx.HTTPStatusError("rate limited", request=request, response=response)
            return "success"

        with patch.object(runner.time, "sleep") as sleep:
            result = runner._qa_call_with_retry(call_qa)

        self.assertEqual(result, "success")
        sleep.assert_called_once_with(7.0)

    def test_qa_retry_recovers_from_5xx(self) -> None:
        request = httpx.Request("POST", "https://example.test/chat/completions")
        response = httpx.Response(503, request=request)
        attempts = 0

        def call_qa() -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx.HTTPStatusError("unavailable", request=request, response=response)
            return "success"

        with patch.object(runner.time, "sleep") as sleep:
            result = runner._qa_call_with_retry(call_qa)

        self.assertEqual(result, "success")
        sleep.assert_called_once_with(2.0)

    def test_qa_retry_recovers_from_read_timeout_with_exponential_backoff(self) -> None:
        request = httpx.Request("POST", "https://example.test/chat/completions")
        attempts = 0

        def call_qa() -> str:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise httpx.ReadTimeout("reader timed out", request=request)
            return "success"

        with patch.object(runner.time, "sleep") as sleep:
            result = runner._qa_call_with_retry(call_qa)

        self.assertEqual(result, "success")
        self.assertEqual(attempts, 3)
        self.assertEqual(sleep.call_args_list, [call(2.0), call(4.0)])

    def test_qa_retry_finds_wrapped_connect_timeout(self) -> None:
        request = httpx.Request("POST", "https://example.test/chat/completions")
        attempts = 0

        def call_qa() -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                try:
                    raise httpx.ConnectTimeout("connect timed out", request=request)
                except httpx.ConnectTimeout as timeout:
                    raise RuntimeError("reader transport failed") from timeout
            return "success"

        with patch.object(runner.time, "sleep") as sleep:
            result = runner._qa_call_with_retry(call_qa)

        self.assertEqual(result, "success")
        self.assertEqual(attempts, 2)
        sleep.assert_called_once_with(2.0)

    def test_qa_retry_does_not_retry_other_timeout_types(self) -> None:
        request = httpx.Request("POST", "https://example.test/chat/completions")
        attempts = 0

        def call_qa() -> str:
            nonlocal attempts
            attempts += 1
            raise httpx.WriteTimeout("write timed out", request=request)

        with patch.object(runner.time, "sleep") as sleep:
            with self.assertRaises(httpx.WriteTimeout):
                runner._qa_call_with_retry(call_qa)

        self.assertEqual(attempts, 1)
        sleep.assert_not_called()

    def test_qa_retry_does_not_retry_http_400_with_nested_read_timeout(self) -> None:
        request = httpx.Request("POST", "https://example.test/chat/completions")
        response = httpx.Response(400, request=request, headers={"Retry-After": "7"})
        attempts = 0

        def call_qa() -> str:
            nonlocal attempts
            attempts += 1
            timeout = httpx.ReadTimeout("stale timeout context", request=request)
            raise httpx.HTTPStatusError("bad request", request=request, response=response) from timeout

        with patch.object(runner.time, "sleep") as sleep:
            with self.assertRaises(httpx.HTTPStatusError):
                runner._qa_call_with_retry(call_qa)

        self.assertEqual(attempts, 1)
        sleep.assert_not_called()

    def test_qa_retry_stops_after_the_configured_timeout_attempts(self) -> None:
        request = httpx.Request("POST", "https://example.test/chat/completions")
        attempts = 0

        def call_qa() -> str:
            nonlocal attempts
            attempts += 1
            raise httpx.ReadTimeout("reader timed out", request=request)

        with patch.object(runner.time, "sleep") as sleep:
            with self.assertRaises(httpx.ReadTimeout):
                runner._qa_call_with_retry(call_qa, max_attempts=2)

        self.assertEqual(attempts, 2)
        sleep.assert_called_once_with(2.0)

    def test_shared_http_retry_honors_nested_retry_after(self) -> None:
        request = httpx.Request("POST", "https://example.test/chat/completions")
        response = httpx.Response(429, request=request, headers={"Retry-After": "7"})
        attempts = 0

        def call_http() -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                try:
                    raise httpx.HTTPStatusError("rate limited", request=request, response=response)
                except httpx.HTTPStatusError as error:
                    raise RuntimeError("wrapped provider failure") from error
            return "success"

        with patch("hl_mem.http_utils.time.sleep") as sleep:
            result = retry_http(call_http, retry_after=True)

        self.assertEqual(result, "success")
        sleep.assert_called_once_with(7.0)

    def test_shared_http_retry_preserves_final_wrapped_timeout_chain(self) -> None:
        request = httpx.Request("POST", "https://example.test/chat/completions")

        def call_http() -> str:
            try:
                raise httpx.ReadTimeout("reader timed out", request=request)
            except httpx.ReadTimeout as error:
                raise RuntimeError("wrapped reader failure") from error

        with patch("hl_mem.http_utils.time.sleep"):
            with self.assertRaises(RuntimeError) as raised:
                retry_http(
                    call_http,
                    max_attempts=2,
                    retry_timeout_types=(httpx.ReadTimeout, httpx.ConnectTimeout),
                )

        self.assertIsInstance(raised.exception.__cause__, httpx.ReadTimeout)

    def test_reader_context_includes_claim_times_and_original_event_source(self) -> None:
        case = runner.normalize_case(_record("case-context"))
        event_id = case.sessions[0].event_id
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE events ("
            "id TEXT PRIMARY KEY,content_json TEXT,occurred_at TEXT,recorded_at TEXT,"
            "event_type TEXT,actor_type TEXT,source_uri TEXT)"
        )
        connection.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?,?)",
            (
                event_id,
                json.dumps(
                    {
                        "text": "user: I bought the blue bicycle at Riverside Cycles.",
                        "benchmark_locator": {"session_id": "session-case-context"},
                    }
                ),
                "2023-05-20T02:21:00+00:00",
                "2023-05-20T02:22:00+00:00",
                "message",
                "user",
                "longmemeval://case-context/session-case-context",
            ),
        )

        prompt = runner._build_reader_user_prompt(
            connection,
            case,
            [
                {
                    "rank": 1,
                    "claim_id": "claim-1",
                    "text": "The user bought a blue bicycle.",
                    "value": "blue bicycle",
                    "status": "active",
                    "valid_from": "2023-05-20T02:21:00+00:00",
                    "valid_to": None,
                    "recorded_from": "2023-05-20T02:22:00+00:00",
                    "recorded_to": None,
                    "evidence_event_ids": [event_id],
                }
            ],
        )

        self.assertIn("Current Date: 2023-05-30T23:40:00+00:00", prompt)
        self.assertIn('"valid_from":"2023-05-20T02:21:00+00:00"', prompt)
        self.assertIn("I bought the blue bicycle at Riverside Cycles", prompt)
        self.assertIn('"session_id":"session-case-context"', prompt)
        self.assertIn('"source_uri":"longmemeval://case-context/session-case-context"', prompt)
        self.assertLessEqual(runner.estimate_tokens(prompt), runner.QA_CONTEXT_TOKEN_BUDGET)
        connection.close()

    def test_reader_context_truncates_large_evidence_within_total_budget(self) -> None:
        case = runner.normalize_case(_record("case-budget"))
        event_id = case.sessions[0].event_id
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE events ("
            "id TEXT PRIMARY KEY,content_json TEXT,occurred_at TEXT,recorded_at TEXT,"
            "event_type TEXT,actor_type TEXT,source_uri TEXT)"
        )
        connection.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?,?)",
            (
                event_id,
                json.dumps({"text": "evidence " * (runner.QA_CONTEXT_TOKEN_BUDGET * 2)}),
                "2023-05-20T02:21:00+00:00",
                "2023-05-20T02:22:00+00:00",
                "message",
                "user",
                None,
            ),
        )

        prompt = runner._build_reader_user_prompt(
            connection,
            case,
            [{"rank": 1, "text": "claim", "evidence_event_ids": [event_id]}],
        )

        self.assertIn("[truncated]", prompt)
        self.assertLessEqual(runner.estimate_tokens(prompt), runner.QA_CONTEXT_TOKEN_BUDGET)
        serialized_events = prompt.split("Original Evidence Events:\n", 1)[1].split("\n\nQuestion:", 1)[0]
        events = json.loads(serialized_events)
        self.assertEqual(len(events), 1)
        self.assertLessEqual(
            runner.estimate_tokens(json.dumps(events[0], ensure_ascii=False, separators=(",", ":"))),
            runner.QA_EVIDENCE_EVENT_TOKEN_LIMIT,
        )
        connection.close()

    def test_windowed_reader_context_prioritizes_matching_turn_and_neighbors(self) -> None:
        case = runner.normalize_case(_record("case-window"))
        event_id = case.sessions[0].event_id
        head_noise = "opening small talk " * (runner.QA_EVIDENCE_EVENT_TOKEN_LIMIT * 2)
        messages = [
            {"role": "user", "content": head_noise},
            {"role": "assistant", "content": "We also discussed an unrelated trip to Rome."},
            {"role": "user", "content": "I had once planned to audition for Hamlet."},
            {
                "role": "user",
                "content": "I actually performed in A Midsummer Night's Dream last summer.",
            },
            {"role": "assistant", "content": "That completed performance sounds memorable."},
        ]
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE events ("
            "id TEXT PRIMARY KEY,content_json TEXT,occurred_at TEXT,recorded_at TEXT,"
            "event_type TEXT,actor_type TEXT,source_uri TEXT)"
        )
        connection.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?,?)",
            (
                event_id,
                json.dumps(
                    {
                        "text": "\n".join(f"{item['role']}: {item['content']}" for item in messages),
                        "messages": messages,
                        "benchmark_locator": {"session_id": "session-case-window"},
                    }
                ),
                "2023-05-20T02:21:00+00:00",
                "2023-05-20T02:22:00+00:00",
                "message",
                "user",
                "longmemeval:case-window:session-case-window",
            ),
        )
        retrieved = [
            {
                "rank": 1,
                "claim_id": "claim-performance",
                "text": "The user performed in A Midsummer Night's Dream last summer.",
                "value": "performed in A Midsummer Night's Dream",
                "evidence_event_ids": [event_id],
            }
        ]

        windowed = runner._build_reader_user_prompt(connection, case, retrieved, context_mode="windowed")
        head = runner._build_reader_user_prompt(connection, case, retrieved, context_mode="head")
        windowed_events = json.loads(windowed.split("Original Evidence Events:\n", 1)[1].split("\n\nQuestion:", 1)[0])
        head_events = json.loads(head.split("Original Evidence Events:\n", 1)[1].split("\n\nQuestion:", 1)[0])

        self.assertEqual(windowed_events[0]["window"]["matched_turn"], 3)
        self.assertEqual(windowed_events[0]["window"]["included_turns"], [2, 3, 4])
        self.assertTrue(windowed_events[0]["content"].startswith("[matched turn 3 user]"))
        self.assertIn("planned to audition for Hamlet", windowed_events[0]["content"])
        self.assertIn("performed in A Midsummer Night's Dream", windowed_events[0]["content"])
        self.assertIn("completed performance", windowed_events[0]["content"])
        self.assertNotIn("opening small talk", windowed_events[0]["content"])
        self.assertIn("opening small talk", head_events[0]["content"])
        self.assertNotIn("A Midsummer Night's Dream", head_events[0]["content"])
        self.assertLessEqual(runner.estimate_tokens(windowed), runner.QA_CONTEXT_TOKEN_BUDGET)
        self.assertLessEqual(
            runner.estimate_tokens(json.dumps(windowed_events[0], ensure_ascii=False, separators=(",", ":"))),
            runner.QA_EVIDENCE_EVENT_TOKEN_LIMIT,
        )
        connection.close()

    def test_windowed_reader_context_falls_back_to_event_text_without_messages(self) -> None:
        case = runner.normalize_case(_record("case-fallback"))
        event_id = case.sessions[0].event_id
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE events ("
            "id TEXT PRIMARY KEY,content_json TEXT,occurred_at TEXT,recorded_at TEXT,"
            "event_type TEXT,actor_type TEXT,source_uri TEXT)"
        )
        connection.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?,?)",
            (
                event_id,
                json.dumps({"text": "user: The fallback evidence says I live in Kyoto."}),
                "2023-05-20T02:21:00+00:00",
                "2023-05-20T02:22:00+00:00",
                "message",
                "user",
                None,
            ),
        )

        prompt = runner._build_reader_user_prompt(
            connection,
            case,
            [{"rank": 1, "text": "The user lives in Kyoto", "evidence_event_ids": [event_id]}],
            context_mode="windowed",
        )

        self.assertIn("The fallback evidence says I live in Kyoto", prompt)
        connection.close()

    def test_windowed_reader_context_combines_far_apart_evidence_turns(self) -> None:
        record = _record("case-multi-window")
        record["question"] = "Where did I move and which instrument did I buy?"
        case = runner.normalize_case(record)
        event_id = case.sessions[0].event_id
        messages = [
            {"role": "assistant", "content": "Let us review your updates."},
            {"role": "user", "content": "I moved to Kyoto for work."},
            *({"role": "assistant", "content": f"Unrelated update {index}."} for index in range(2, 8)),
            {"role": "user", "content": "I bought a cello after the move."},
            {"role": "assistant", "content": "Thanks for both updates."},
        ]
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE events ("
            "id TEXT PRIMARY KEY,content_json TEXT,occurred_at TEXT,recorded_at TEXT,"
            "event_type TEXT,actor_type TEXT,source_uri TEXT)"
        )
        connection.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?,?)",
            (
                event_id,
                json.dumps({"text": "unused head", "messages": messages}),
                "2023-05-20T02:21:00+00:00",
                "2023-05-20T02:22:00+00:00",
                "message",
                "user",
                None,
            ),
        )
        retrieved = [
            {
                "rank": 1,
                "text": "The user moved to Kyoto for work.",
                "value": "moved to Kyoto for work",
                "evidence_event_ids": [event_id],
            },
            {
                "rank": 2,
                "text": "The user bought a cello after moving.",
                "value": "bought a cello after the move",
                "evidence_event_ids": [event_id],
            },
        ]

        prompt = runner._build_reader_user_prompt(connection, case, retrieved, context_mode="windowed")
        events_json = prompt.split("Original Evidence Events:\n", 1)[1].split("\n\nQuestion:", 1)[0]
        event = json.loads(events_json)[0]

        self.assertIn("I moved to Kyoto for work.", event["content"])
        self.assertIn("I bought a cello after the move.", event["content"])
        self.assertEqual(event["window"]["matched_turns"], [1, 8])
        self.assertLessEqual(runner.estimate_tokens(prompt), runner.QA_CONTEXT_TOKEN_BUDGET)
        connection.close()

    def test_windowed_reader_context_centers_a_long_matching_turn_on_the_claim_span(self) -> None:
        case = runner.normalize_case(_record("case-span"))
        event_id = case.sessions[0].event_id
        answer_sentence = "The answer-bearing fact is that I moved to Kyoto in April."
        messages = [
            {"role": "assistant", "content": "What changed?"},
            {
                "role": "user",
                "content": ("irrelevant preface " * 500) + answer_sentence + (" trailing detail" * 500),
            },
            {"role": "assistant", "content": "Thanks for the update."},
        ]
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE events ("
            "id TEXT PRIMARY KEY,content_json TEXT,occurred_at TEXT,recorded_at TEXT,"
            "event_type TEXT,actor_type TEXT,source_uri TEXT)"
        )
        connection.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?,?)",
            (
                event_id,
                json.dumps({"text": "unused head", "messages": messages}),
                "2023-05-20T02:21:00+00:00",
                "2023-05-20T02:22:00+00:00",
                "message",
                "user",
                None,
            ),
        )

        prompt = runner._build_reader_user_prompt(
            connection,
            case,
            [
                {
                    "rank": 1,
                    "text": "The user moved to Kyoto in April.",
                    "value": "moved to Kyoto in April",
                    "evidence_event_ids": [event_id],
                }
            ],
            context_mode="windowed",
        )

        self.assertIn(answer_sentence, prompt)
        self.assertIn("[earlier text omitted]", prompt)
        self.assertIn("[later text omitted]", prompt)
        self.assertLessEqual(runner.estimate_tokens(prompt), runner.QA_CONTEXT_TOKEN_BUDGET)
        connection.close()

    def test_windowed_reader_context_uses_question_to_focus_a_paraphrased_long_turn(self) -> None:
        record = _record("case-question-span")
        record["question"] = "Which city did I relocate to after leaving Osaka?"
        case = runner.normalize_case(record)
        event_id = case.sessions[0].event_id
        answer_sentence = "After leaving Osaka, I relocated to Kyoto for work."
        messages = [
            {
                "role": "user",
                "content": ("unrelated housing preface " * 500) + answer_sentence + (" trailing detail" * 500),
            }
        ]
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE events ("
            "id TEXT PRIMARY KEY,content_json TEXT,occurred_at TEXT,recorded_at TEXT,"
            "event_type TEXT,actor_type TEXT,source_uri TEXT)"
        )
        connection.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?,?)",
            (
                event_id,
                json.dumps({"text": "unused head", "messages": messages}),
                "2023-05-20T02:21:00+00:00",
                "2023-05-20T02:22:00+00:00",
                "message",
                "user",
                None,
            ),
        )

        prompt = runner._build_reader_user_prompt(
            connection,
            case,
            [
                {
                    "rank": 1,
                    "text": "The user changed residence for employment.",
                    "value": "a residential change",
                    "evidence_event_ids": [event_id],
                }
            ],
            context_mode="windowed",
        )

        self.assertIn(answer_sentence, prompt)
        self.assertIn("[earlier text omitted]", prompt)
        self.assertIn("[later text omitted]", prompt)
        connection.close()

    def test_reader_context_structurally_caps_claim_metadata_and_preserves_question(self) -> None:
        record = _record("case-claim-budget")
        record["question"] = "What is the complete question that must remain at the end?"
        case = runner.normalize_case(record)
        retrieved = [
            {
                "rank": rank,
                "claim_id": f"claim-{rank}",
                "text": f"claim text {rank} " * runner.QA_CLAIM_FIELD_TOKEN_LIMIT,
                "value": f"claim value {rank} " * runner.QA_CLAIM_FIELD_TOKEN_LIMIT,
                "evidence_event_ids": [f"event-{rank}-{'x' * 80}-{index}" for index in range(500)],
            }
            for rank in range(1, 21)
        ]

        prompt = runner._build_reader_user_prompt(None, case, retrieved, context_mode="windowed")
        claims_json = prompt.split("Memory Claims:\n", 1)[1].split("\n\nOriginal Evidence Events:", 1)[0]
        claims = json.loads(claims_json)

        self.assertGreater(len(claims), 0)
        self.assertLess(len(claims), len(retrieved))
        self.assertTrue(any(int(claim.get("evidence_event_ids_omitted", 0)) > 0 for claim in claims))
        self.assertTrue(prompt.endswith(f"Question: {case.question}"))
        self.assertLessEqual(runner.estimate_tokens(prompt), runner.QA_CONTEXT_TOKEN_BUDGET)

    def test_run_qa_uses_plain_chat_and_retries_http_errors(self) -> None:
        case = runner.normalize_case(_record("case-qa"))
        requests: list[httpx.Request] = []

        def handle_request(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(503, json={"error": "temporarily unavailable"})
            if len(requests) == 2:
                return httpx.Response(
                    200,
                    json={
                        "choices": [{"message": {"content": "Answer for case-qa"}}],
                        "usage": {"total_tokens": 11},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": '```json\n{"correct": true, "reason": "The answers match."}\n```'}}
                    ],
                    "usage": {"total_tokens": 13},
                },
            )

        clients = [httpx.Client(transport=httpx.MockTransport(handle_request)) for _ in range(3)]
        settings = Settings(
            llm_api_key="settings-key",
            llm_base_url="https://coding.dashscope.aliyuncs.com/v1",
        )

        with (
            patch.dict(
                os.environ,
                {"LLM_API_KEY": "environment-key", "HL_MEM_EVAL_QA_MODEL": "qa-override"},
                clear=True,
            ),
            patch.object(runner.httpx, "Client", side_effect=clients),
            patch.object(
                runner,
                "make_llm_client",
                side_effect=AssertionError("QA must not use the structured LLM client"),
                create=True,
            ),
            patch.object(runner.time, "sleep") as sleep,
        ):
            result = runner._run_qa(
                None,
                case,
                [{"rank": 1, "text": "Memory for case-qa"}],
                settings,
            )

        self.assertEqual(
            result,
            {
                "model": "qa-override",
                "predicted_answer": "Answer for case-qa",
                "correct": True,
                "reason": "The answers match.",
                "usage": {"reader_tokens": 11, "judge_tokens": 13, "total_tokens": 24},
            },
        )
        self.assertEqual(len(requests), 3)
        self.assertEqual(sleep.call_args_list, [call(2.0)])
        for request in requests:
            self.assertEqual(str(request.url), "https://coding.dashscope.aliyuncs.com/v1/chat/completions")
            self.assertEqual(request.headers["Authorization"], "Bearer environment-key")
            payload = json.loads(request.content)
            self.assertEqual(payload["model"], "qa-override")
            self.assertEqual(payload["max_tokens"], 512)
            self.assertNotIn("response_format", payload)
        self.assertEqual([json.loads(request.content)["temperature"] for request in requests], [0.1, 0.1, 0.0])
        reader_payload = json.loads(requests[1].content)
        self.assertIn("candidate answer", reader_payload["messages"][0]["content"])
        self.assertIn("relation", reader_payload["messages"][0]["content"])
        self.assertIn("audition", reader_payload["messages"][0]["content"])
        self.assertIn("participation", reader_payload["messages"][0]["content"])
        self.assertIn("location, travel duration, and distance", reader_payload["messages"][0]["content"])
        self.assertIn("planned or intended", reader_payload["messages"][0]["content"])
        self.assertIn("actually executed", reader_payload["messages"][0]["content"])
        self.assertIn("genuinely insufficient", reader_payload["messages"][0]["content"])
        self.assertIn("information is unavailable", reader_payload["messages"][0]["content"])
        self.assertIn("Return only the final answer", reader_payload["messages"][0]["content"])
        self.assertIn("Current Date: 2023-05-30T23:40:00+00:00", reader_payload["messages"][1]["content"])
        judge_payload = json.loads(requests[2].content)
        self.assertIn("official-style LongMemEval", judge_payload["messages"][0]["content"])

    def test_resume_rejects_a_different_reader_context_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dataset = Path(directory) / "dataset.json"
            dataset.write_text(json.dumps([_record("case-1")]), encoding="utf-8")
            dataset_sha256 = hashlib.sha256(dataset.read_bytes()).hexdigest()
            report = _shard_report(
                dataset_sha256,
                [_case_result("case-1")],
                reader_context_mode="head",
            )
            args = runner.parse_args(["--dataset", str(dataset), "--resume"])
            args.dataset_sha256 = dataset_sha256
            settings = Settings(
                llm_model="qwen3.7-plus",
                llm_provider="dashscope",
                embedding_model="qwen3.7-text-embedding",
                embedding_dim=2048,
                embedding_api_mode="native",
                embedding_text_type=None,
            )

            with self.assertRaisesRegex(ValueError, "reader_context_mode"):
                runner._validate_resume_report(report, args, settings)

    def test_resume_history_does_not_reopen_circuit_breaker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "dataset.json"
            output = root / "result.json"
            dataset.write_text(json.dumps([_record(f"case-{index}") for index in range(6)]), encoding="utf-8")
            dataset_sha256 = hashlib.sha256(dataset.read_bytes()).hexdigest()
            failed = [
                _case_result(
                    f"case-{index}",
                    error="HTTPStatusError: unauthorized",
                    qa_evaluated=False,
                )
                for index in range(5)
            ]
            output.write_text(
                json.dumps(_shard_report(dataset_sha256, failed, qa_enabled=False)),
                encoding="utf-8",
            )
            settings = Settings(
                embedding_model="qwen3.7-text-embedding",
                embedding_dim=2048,
                embedding_api_mode="native",
                embedding_text_type=None,
            )

            def run_case(case: runner.LongMemEvalCase, *_args: object, **_kwargs: object) -> dict[str, object]:
                return _case_result(case.case_id, qa_evaluated=False)

            with (
                patch.object(runner, "load_settings", return_value=settings),
                patch.object(runner, "_validate_production_settings"),
                patch.object(runner, "initialize_process"),
                patch.object(runner, "make_embedder", return_value=object()),
                patch.object(runner, "make_reranker", return_value=None),
                patch.object(runner, "_run_case", side_effect=run_case) as run,
            ):
                exit_code = runner.main(
                    [
                        "--dataset",
                        str(dataset),
                        "--output",
                        str(output),
                        "--resume",
                        "--no-qa",
                    ]
                )

            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0].case_id, "case-5")
        self.assertEqual(report["status"], "completed")


class LongMemEvalMergeTests(unittest.TestCase):
    def _write_dataset(self, root: Path) -> tuple[Path, str]:
        dataset = root / "dataset.json"
        dataset.write_text(json.dumps([_record("case-1"), _record("case-2")]), encoding="utf-8")
        return dataset, hashlib.sha256(dataset.read_bytes()).hexdigest()

    def test_merge_sorts_in_dataset_order_and_reaggregates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset, dataset_sha256 = self._write_dataset(root)
            (root / "shard-b.json").write_text(
                json.dumps(_shard_report(dataset_sha256, [_case_result("case-2", correct=False)])),
                encoding="utf-8",
            )
            (root / "shard-a.json").write_text(
                json.dumps(_shard_report(dataset_sha256, [_case_result("case-1")])),
                encoding="utf-8",
            )

            report = merger.merge_reports(
                input_dir=root,
                pattern="shard-*.json",
                dataset=dataset,
                require_cases=2,
                require_no_errors=True,
            )

        self.assertEqual([case["case_id"] for case in report["cases"]], ["case-1", "case-2"])
        self.assertEqual(report["metrics"]["overall"]["cases"], 2)
        self.assertEqual(report["metrics"]["overall"]["qa_accuracy"], 0.5)

    def test_merge_rejects_duplicate_case_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset, dataset_sha256 = self._write_dataset(root)
            for name in ("shard-a.json", "shard-b.json"):
                (root / name).write_text(
                    json.dumps(_shard_report(dataset_sha256, [_case_result("case-1")])),
                    encoding="utf-8",
                )

            with self.assertRaisesRegex(ValueError, "duplicate case_id"):
                merger.merge_reports(input_dir=root, pattern="shard-*.json", dataset=dataset)

    def test_merge_rejects_inconsistent_embedder_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset, dataset_sha256 = self._write_dataset(root)
            first = _shard_report(dataset_sha256, [_case_result("case-1")])
            second = _shard_report(dataset_sha256, [_case_result("case-2")])
            second["run"]["models"]["embedder"] = "different-model"  # type: ignore[index]
            (root / "shard-a.json").write_text(json.dumps(first), encoding="utf-8")
            (root / "shard-b.json").write_text(json.dumps(second), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "configuration mismatch"):
                merger.merge_reports(input_dir=root, pattern="shard-*.json", dataset=dataset)

    def test_merge_rejects_mixed_qa_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset, dataset_sha256 = self._write_dataset(root)
            first = _shard_report(dataset_sha256, [_case_result("case-1")])
            second = _shard_report(
                dataset_sha256,
                [_case_result("case-2", qa_evaluated=False)],
                qa_enabled=False,
            )
            (root / "shard-a.json").write_text(json.dumps(first), encoding="utf-8")
            (root / "shard-b.json").write_text(json.dumps(second), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "configuration mismatch"):
                merger.merge_reports(input_dir=root, pattern="shard-*.json", dataset=dataset)

    def test_merge_rejects_mixed_reader_context_modes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset, dataset_sha256 = self._write_dataset(root)
            first = _shard_report(dataset_sha256, [_case_result("case-1")])
            second = _shard_report(
                dataset_sha256,
                [_case_result("case-2")],
                reader_context_mode="head",
            )
            (root / "shard-a.json").write_text(json.dumps(first), encoding="utf-8")
            (root / "shard-b.json").write_text(json.dumps(second), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "configuration mismatch"):
                merger.merge_reports(input_dir=root, pattern="shard-*.json", dataset=dataset)

    def test_merge_normalizes_legacy_and_explicit_head_context_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset, dataset_sha256 = self._write_dataset(root)
            legacy = _shard_report(
                dataset_sha256,
                [_case_result("case-1")],
                reader_context_mode="head",
            )
            del legacy["run"]["reader_context_mode"]  # type: ignore[index]
            explicit = _shard_report(
                dataset_sha256,
                [_case_result("case-2")],
                reader_context_mode="head",
            )
            (root / "shard-a.json").write_text(json.dumps(legacy), encoding="utf-8")
            (root / "shard-b.json").write_text(json.dumps(explicit), encoding="utf-8")

            report = merger.merge_reports(input_dir=root, pattern="shard-*.json", dataset=dataset)

        self.assertEqual(report["run"]["reader_context_mode"], "head")

    def test_merge_rejects_case_without_error_field(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset, dataset_sha256 = self._write_dataset(root)
            malformed = _case_result("case-1")
            del malformed["error"]
            (root / "shard-a.json").write_text(
                json.dumps(_shard_report(dataset_sha256, [malformed])),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "missing required field 'error'"):
                merger.merge_reports(input_dir=root, pattern="shard-*.json", dataset=dataset)


if __name__ == "__main__":
    unittest.main()
