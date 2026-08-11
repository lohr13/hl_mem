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
from evaluation.tools.longmemeval import qa_client, reader_context
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
            "package_version": f"v{runner.__version__}",
            "limit": run_limit,
            "offset": 0,
            "qa_enabled": qa_enabled,
            "reader_context_mode": reader_context_mode,
            "models": {
                "extractor": "qwen3.7-plus",
                "extractor_provider": "dashscope",
                "extractor_effective_provider": "dashscope",
                "extractor_base_url": "https://coding.dashscope.aliyuncs.com/v1",
                "extractor_structured_mode": "json_object",
                "extractor_thinking": False,
                "extractor_version": runner.LLM_EXTRACTOR_VERSION,
                "extraction_fragment_protocol": runner.EXTRACTION_FRAGMENT_PROTOCOL_VERSION,
                "extraction_chunk_target_chars": 12_000,
                "extraction_chunk_overlap_turns": 2,
                "reader_context_protocol": runner.READER_CONTEXT_PROTOCOL_VERSION,
                "query_expansion_model": "qwen3.7-plus",
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
    def test_reader_thinking_is_enabled_only_for_multi_session(self) -> None:
        self.assertTrue(qa_client.reader_thinking_enabled("multi-session"))
        for question_type in (
            "single-session-user",
            "single-session-assistant",
            "single-session-preference",
            "knowledge-update",
            "temporal-reasoning",
        ):
            with self.subTest(question_type=question_type):
                self.assertFalse(qa_client.reader_thinking_enabled(question_type))

    def test_resume_defaults_missing_claim_inflation_metrics_without_inventing_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "legacy.json"
            output.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "benchmark": "LongMemEval-S",
                        "cases": [
                            {
                                "case_id": "legacy",
                                "ingest": {
                                    "sessions": 1,
                                    "events": 2,
                                    "extracted_claims_per_event": 3.0,
                                    "stored_claims_per_event": 2.0,
                                    "stored_claims_per_session": 4.0,
                                    "adjacent_restatement_candidates": 7,
                                    "adjacent_restatement_definition": "legacy write-result metric",
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            _, cases = runner._load_resume_report(output)

        ingest = cases[0]["ingest"]
        self.assertEqual(ingest["claim_inflation_diagnostics_status"], "unavailable_legacy_resume")
        self.assertIsNone(ingest["stored_claims"])
        self.assertIsNone(ingest["extracted_claims_per_event"])
        self.assertIsNone(ingest["stored_claims_per_event"])
        self.assertIsNone(ingest["stored_claims_per_session"])
        self.assertIsNone(ingest["adjacent_restatement_candidates"])
        self.assertIsNone(ingest["adjacent_restatement_definition"])

    def test_assistant_raw_fallback_has_narrow_trigger(self) -> None:
        assistant_record = _record("case-assistant-trigger")
        assistant_record["question_type"] = "single-session-assistant"
        assistant_case = runner.normalize_case(assistant_record)
        explicit_record = _record("case-explicit-trigger")
        explicit_record["question"] = "In the table you provided earlier, what was the seventh item?"
        explicit_case = runner.normalize_case(explicit_record)
        factual_case = runner.normalize_case(_record("case-no-trigger"))

        self.assertTrue(reader_context.assistant_raw_fallback_requested(assistant_case))
        self.assertTrue(reader_context.assistant_raw_fallback_requested(explicit_case))
        self.assertFalse(reader_context.assistant_raw_fallback_requested(factual_case))

    def test_assistant_raw_fallback_is_namespace_scoped_or_fts_and_budgeted(self) -> None:
        record = _record("case-raw-assistant")
        record["question_type"] = "single-session-assistant"
        record["question"] = "What was the seventh home-based job you provided in the list?"
        case = runner.normalize_case(record)
        target_event_id = "target-assistant-turn"
        target_text = (
            "Here are home-based jobs:\n"
            "1. Virtual Assistant\n2. Tutor\n3. Bookkeeper\n4. Designer\n"
            "5. Translator\n6. Editor\n7. Transcriptionist"
        )
        leaked_text = "seventh home-based job provided list: Secret cross-tenant answer"

        with tempfile.TemporaryDirectory() as directory:
            database = runner.Database(Path(directory) / "raw.db", settings=Settings.for_test())
            connection = database.open()
            service = runner.IngestService(connection)
            for event in (
                {
                    "id": "cross-tenant",
                    "tenant_id": "eval:longmemeval:other-case",
                    "session_id": "leaked-session",
                    "event_type": "message",
                    "actor_type": "assistant",
                    "content": {"text": leaked_text},
                },
                {
                    "id": target_event_id,
                    "tenant_id": case.namespace,
                    "session_id": "answer-session",
                    "event_type": "message",
                    "actor_type": "assistant",
                    "content": {
                        "text": target_text,
                        "benchmark_locator": {
                            "session_id": "answer-session",
                            "turn_index": 1,
                            "span": [1, 2],
                            "source_role": "assistant",
                        },
                    },
                },
                {
                    "id": "same-namespace-user",
                    "tenant_id": case.namespace,
                    "session_id": "user-session",
                    "event_type": "message",
                    "actor_type": "user",
                    "content": {"text": "seventh home-based job provided list"},
                },
            ):
                service.ingest_event(event)

            prompt = runner._build_reader_user_prompt(
                connection,
                case,
                [
                    {
                        "rank": 1,
                        "claim_id": "weak-claim",
                        "text": "A list of remote work existed.",
                        "value": "remote work list",
                        "evidence_event_ids": [target_event_id],
                    }
                ],
                context_mode="windowed",
            )
            database.close()

        events_json = prompt.split("Original Evidence Events:\n", 1)[1].split("\n\nQuestion:", 1)[0]
        events = json.loads(events_json)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_id"], target_event_id)
        self.assertEqual(events[0]["retrieval_source"], "assistant_raw_fallback")
        self.assertIn("occurred_at", events[0])
        self.assertNotIn("recorded_at", events[0])
        self.assertIn("7. Transcriptionist", events[0]["content"])
        self.assertNotIn("Secret cross-tenant answer", prompt)
        self.assertLessEqual(
            runner.estimate_tokens(json.dumps(events[0], ensure_ascii=False, separators=(",", ":"))),
            runner.QA_EVIDENCE_EVENT_TOKEN_LIMIT,
        )
        self.assertLessEqual(runner.estimate_tokens(prompt), runner.QA_CONTEXT_TOKEN_BUDGET)

    def test_assistant_ordinal_fallback_pairs_question_turn_with_next_assistant(self) -> None:
        record = _record("case-ordinal-pair")
        record["question_type"] = "single-session-assistant"
        record["question"] = "What was the seventh home-based job in the numbered list you provided?"
        case = runner.normalize_case(record)
        target_text = (
            "Here is the numbered list of home-based jobs:\n"
            "1. Tutor\n2. Editor\n3. Designer\n4. Bookkeeper\n"
            "5. Translator\n6. Researcher\n7. Transcriptionist"
        )

        with tempfile.TemporaryDirectory() as directory:
            database = runner.Database(Path(directory) / "ordinal.db", settings=Settings.for_test())
            connection = database.open()
            service = runner.IngestService(connection)
            for event in (
                {
                    "id": "matched-user-turn",
                    "tenant_id": case.namespace,
                    "session_id": "target-session",
                    "event_type": "message",
                    "actor_type": "user",
                    "content": {
                        "text": "Please provide a numbered list of home-based jobs.",
                        "benchmark_locator": {"session_id": "target-session", "turn_index": 4},
                    },
                },
                {
                    "id": "paired-assistant-turn",
                    "tenant_id": case.namespace,
                    "session_id": "target-session",
                    "event_type": "message",
                    "actor_type": "assistant",
                    "content": {
                        "text": target_text,
                        "benchmark_locator": {"session_id": "target-session", "turn_index": 5},
                    },
                },
                {
                    "id": "fts-distractor",
                    "tenant_id": case.namespace,
                    "session_id": "other-session",
                    "event_type": "message",
                    "actor_type": "assistant",
                    "content": {
                        "text": (
                            "What was the seventh home-based job in the numbered list you provided?\n" "7. Wrong answer"
                        ),
                        "benchmark_locator": {"session_id": "other-session", "turn_index": 9},
                    },
                },
            ):
                service.ingest_event(event)

            event = reader_context.load_assistant_raw_fallback(connection, case)
            database.close()

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event["event_id"], "paired-assistant-turn")
        self.assertEqual(event["window"]["mode"], "assistant_raw_ordinal_pair")
        self.assertIn("7. Transcriptionist", event["content"])
        self.assertNotIn("Wrong answer", event["content"])

    def test_reader_prompt_keeps_factual_questions_closed_book(self) -> None:
        case = runner.normalize_case(_record("case-factual-reader"))

        prompt = runner._reader_system_prompt(case)

        self.assertIn("Do not invent missing proper nouns", prompt)
        self.assertIn("use occurred and valid times plus Current Date", prompt)
        self.assertNotIn("recorded", prompt.casefold())
        self.assertNotIn("synthesize a recommendation", prompt)
        self.assertNotIn("For count or sum questions", prompt)
        self.assertIn("Count repeated phrasings of the same fact only once", prompt)
        self.assertIn("number, date, weekday, entity, or qualifier differs", prompt)

    def test_reader_prompt_allows_grounded_preference_recommendation_synthesis(self) -> None:
        record = _record("case-preference-reader")
        record["question_type"] = "single-session-preference"
        record["question"] = "Can you suggest a hotel for my upcoming trip to Miami?"
        case = runner.normalize_case(record)

        prompt = runner._reader_system_prompt(case)

        self.assertIn("treat the memories as constraints", prompt)
        self.assertIn("synthesize a recommendation", prompt)
        self.assertIn("specific proper noun is absent", prompt)
        self.assertIn("explicitly use the known preferences", prompt)

    def test_reader_prompt_does_not_enable_synthesis_for_preference_fact_lookup(self) -> None:
        record = _record("case-preference-fact")
        record["question_type"] = "single-session-preference"
        record["question"] = "Which video editor do I prefer?"
        case = runner.normalize_case(record)

        prompt = runner._reader_system_prompt(case)

        self.assertNotIn("synthesize a recommendation", prompt)

    def test_evaluation_preference_first_reserves_only_one_claim(self) -> None:
        record = _record("case-preference-reserve")
        record["question_type"] = "single-session-preference"
        case = runner.normalize_case(record)
        production_order = [
            {"id": "preference-1", "canonical_slot": "preference.other", "score": 0.30},
            {"id": "preference-2", "canonical_slot": "preference.other", "score": 0.20},
            {"id": "preference-3", "canonical_slot": "preference.other", "score": 0.10},
            *(
                {"id": f"global-{index}", "canonical_slot": "profile.other", "score": 1.0 - index / 100}
                for index in range(9)
            ),
        ]

        selected = runner._preference_first(production_order, 10, case)

        self.assertEqual(selected[0]["id"], "preference-1")
        self.assertEqual([item["id"] for item in selected[1:]], [f"global-{index}" for index in range(9)])

    def test_evaluation_preference_overfetch_is_narrowly_routed(self) -> None:
        preference_record = _record("case-preference-limit")
        preference_record["question_type"] = "single-session-preference"
        preference_case = runner.normalize_case(preference_record)
        factual_case = runner.normalize_case(_record("case-factual-limit"))

        self.assertEqual(runner.RETRIEVAL_KS, (1, 5, 10))
        self.assertEqual(runner._reader_recall_limit(preference_case), 12)
        self.assertEqual(runner._reader_recall_limit(factual_case), 10)

    def test_temporal_reader_selects_effective_baseline_before_offset(self) -> None:
        record = _record("case-temporal-reader")
        record["question_type"] = "temporal-reasoning"
        record["question"] = "What time do I wake up on Tuesdays and Thursdays?"
        case = runner.normalize_case(record)

        prompt = runner._reader_system_prompt(case)

        self.assertIn("latest baseline effective at the question time", prompt)
        self.assertIn("then apply weekday conditions or relative offsets", prompt)
        self.assertIn("never apply an offset to a superseded baseline", prompt)
        self.assertIn("historical question", prompt)
        self.assertIn("never import a later current value", prompt)

    def test_count_reader_enumerates_deduplicates_then_totals(self) -> None:
        record = _record("case-count-reader")
        record["question"] = "How many different fitness activities did I mention?"
        case = runner.normalize_case(record)

        prompt = runner._reader_system_prompt(case)

        self.assertIn("enumerate every record", prompt)
        self.assertIn("each item is counted once", prompt)
        self.assertIn("only then compute the total", prompt)

    def test_knowledge_update_reader_prefers_latest_valid_statement(self) -> None:
        record = _record("case-update-reader")
        record["question_type"] = "knowledge-update"
        case = runner.normalize_case(record)

        prompt = runner._reader_system_prompt(case)

        self.assertIn("latest statement that is valid at the question time", prompt)
        self.assertIn("history only", prompt)
        self.assertIn("must never override the updated value", prompt)

    def test_ingest_persists_each_turn_with_real_speaker_and_session_span(self) -> None:
        record = _record("case-speakers")
        record["haystack_sessions"] = [
            [
                {"role": "user", "content": "Please build a schedule."},
                {"role": "assistant", "content": "Sunday | Admon | 8am-4pm"},
            ]
        ]
        case = runner.normalize_case(record)
        settings = Settings.for_test()

        class RecordingExtractor:
            last_input_tokens = 0
            last_output_tokens = 0
            last_usage_tokens = 0

            def __init__(self) -> None:
                self.calls: list[tuple[dict[str, object], dict[str, object]]] = []

            def extract(
                self,
                content: dict[str, object],
                context: dict[str, object] | None = None,
            ) -> list[object]:
                self.calls.append((content, context or {}))
                return []

        extractor = RecordingExtractor()
        with tempfile.TemporaryDirectory() as directory:
            database = runner.Database(Path(directory) / "speaker.db", settings=settings)
            connection = database.open()
            with patch.object(runner, "make_extractor", return_value=extractor):
                stats = runner._ingest_case(
                    connection,
                    case,
                    settings,
                    object(),
                    case_number=1,
                    total_hint="1",
                )
            rows = connection.execute(
                "SELECT id,session_id,actor_type,content_json,metadata_json FROM events ORDER BY id"
            ).fetchall()
            database.close()

        self.assertEqual(stats["sessions"], 1)
        self.assertEqual(stats["events"], 2)
        self.assertEqual([row["actor_type"] for row in rows], ["user", "assistant"])
        self.assertEqual([row["session_id"] for row in rows], ["session-case-speakers"] * 2)
        self.assertEqual(len(extractor.calls), 2)
        for index, row in enumerate(rows):
            content = json.loads(row["content_json"])
            metadata = json.loads(row["metadata_json"])
            locator = content["benchmark_locator"]
            self.assertEqual(row["id"], runner._turn_event_id(case.sessions[0].event_id, index))
            self.assertEqual(locator["session_id"], "session-case-speakers")
            self.assertEqual(locator["turn_index"], index)
            self.assertEqual(locator["span"], [index, index + 1])
            self.assertEqual(locator["source_role"], ["user", "assistant"][index])
            self.assertEqual(metadata["turn_index"], index)

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

    def test_resume_retries_429_case_after_quota_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "dataset.json"
            output = root / "result.json"
            dataset.write_text(json.dumps([_record("case-1"), _record("case-2")]), encoding="utf-8")
            dataset_sha256 = hashlib.sha256(dataset.read_bytes()).hexdigest()
            limited = _case_result(
                "case-2",
                error="HTTPStatusError: too many requests",
                qa_evaluated=False,
            )
            limited["error_type"] = "http_429"
            output.write_text(
                json.dumps(
                    _shard_report(
                        dataset_sha256,
                        [_case_result("case-1", qa_evaluated=False), limited],
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
        self.assertEqual([case["case_id"] for case in report["cases"]], ["case-1", "case-2"])
        self.assertIsNone(report["cases"][1]["error"])

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

    def test_case_result_persists_sanitized_http_diagnostics(self) -> None:
        case = runner.normalize_case(_record("case-http-diagnostics"))
        opaque_secret = "provider-secret-without-standard-prefix"
        request = httpx.Request("POST", "https://example.test/chat/completions")
        response = httpx.Response(
            429,
            request=request,
            json={
                "error": {
                    "code": "RateLimitExceeded",
                    "message": (
                        'api_key="sk-secret123456" Authorization: Bearer token-value '
                        f"opaque_token={opaque_secret} " + "x" * 600
                    ),
                },
                "Access_Token": "access-value",
                "PASSWORD": "password-value",
                "token": "generic-token-value",
                "request_id": "body-request-id",
            },
        )
        status_error = httpx.HTTPStatusError(
            'rate limited password="error-password" token=error-token sk-error123456',
            request=request,
            response=response,
        )

        class FailingDatabase:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def open(self) -> object:
                raise RuntimeError(
                    'wrapped extraction failure password="error password secret" '
                    'token="error token secret" sk-error123456'
                ) from status_error

            def close(self) -> None:
                pass

        with (
            patch.object(runner, "Database", FailingDatabase),
            patch.object(runner, "_remove_case_artifacts"),
        ):
            result = runner._run_case(
                case,
                Settings(llm_api_key=opaque_secret),
                object(),
                None,
                skip_ingest=False,
                run_qa=False,
                clean=False,
                case_number=1,
                total_hint="1",
            )

        diagnostics = result["error_diagnostics"]
        self.assertEqual(diagnostics["http_status"], 429)
        self.assertEqual(diagnostics["provider_code"], "RateLimitExceeded")
        self.assertEqual(diagnostics["request_id"], "body-request-id")
        self.assertLessEqual(len(diagnostics["response_body"]), 500)
        for secret in (
            "secret123456",
            "token-value",
            opaque_secret,
            "access-value",
            "password-value",
            "generic-token-value",
            "error password secret",
            "password secret",
            "error token secret",
            "token secret",
            "error123456",
        ):
            self.assertNotIn(secret, diagnostics["response_body"])
            self.assertNotIn(secret, result["error"])

    def test_http_diagnostics_sanitizes_non_json_response_body(self) -> None:
        request = httpx.Request("POST", "https://example.test/chat/completions")
        response = httpx.Response(
            400,
            request=request,
            text=(
                "PASSWORD='plain password secret' token=\"plain token secret\" "
                "access-token: access-token-value credential=credential-value sk-live12345678"
            ),
        )
        error = httpx.HTTPStatusError("bad request", request=request, response=response)

        diagnostics = runner._evaluation_http_error_diagnostics(error)

        self.assertEqual(diagnostics["http_status"], 400)
        for secret in (
            "plain password secret",
            "password secret",
            "plain token secret",
            "token secret",
            "access-token-value",
            "credential-value",
            "live12345678",
        ):
            self.assertNotIn(secret, diagnostics["response_body"])

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

    def test_reader_context_excludes_ingest_times_but_keeps_benchmark_times_and_source(self) -> None:
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
                "2026-08-10T02:22:00+00:00",
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
                    "recorded_from": "2026-08-10T02:22:00+00:00",
                    "recorded_to": "2026-08-11T02:22:00+00:00",
                    "evidence_event_ids": [event_id],
                }
            ],
        )

        claims_json = prompt.split("Memory Claims:\n", 1)[1].split("\n\nOriginal Evidence Events:", 1)[0]
        claims = json.loads(claims_json)
        events_json = prompt.split("Original Evidence Events:\n", 1)[1].split("\n\nQuestion:", 1)[0]
        events = json.loads(events_json)
        self.assertIn("Current Date: 2023-05-30T23:40:00+00:00", prompt)
        self.assertEqual(claims[0]["valid_from"], "2023-05-20T02:21:00+00:00")
        self.assertNotIn("recorded_from", claims[0])
        self.assertNotIn("recorded_to", claims[0])
        self.assertEqual(events[0]["occurred_at"], "2023-05-20T02:21:00+00:00")
        self.assertNotIn("recorded_at", events[0])
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

    def test_windowed_reader_context_reconstructs_adjacent_turn_events_by_session(self) -> None:
        record = _record("case-session-window")
        record["question"] = "What type of rice is my favorite?"
        case = runner.normalize_case(record)
        session_id = "rice-session"
        messages = [
            {"role": "user", "content": "Unrelated opening."},
            {"role": "assistant", "content": "Unrelated response."},
            {"role": "user", "content": "More unrelated context."},
            {"role": "assistant", "content": "Another unrelated response."},
            {
                "role": "user",
                "content": "I cook Japanese dishes with my favorite Japanese short-grain rice.",
            },
            {"role": "assistant", "content": "That rice works well for Japanese dishes."},
            {"role": "user", "content": "I plan to make onigiri for lunch."},
            {"role": "assistant", "content": "Use sticky rice for shaping onigiri."},
            {"role": "user", "content": "I will try shaping onigiri again."},
            {"role": "assistant", "content": "Store each onigiri separately."},
            {"role": "user", "content": "I will wrap them individually."},
            {"role": "assistant", "content": "They keep for several days."},
        ]
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE events ("
            "id TEXT PRIMARY KEY,tenant_id TEXT,session_id TEXT,content_json TEXT,"
            "occurred_at TEXT,recorded_at TEXT,event_type TEXT,actor_type TEXT,source_uri TEXT)"
        )
        event_ids: list[str] = []
        for turn_index, message in enumerate(messages):
            event_id = f"rice-turn-{turn_index}"
            event_ids.append(event_id)
            connection.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    case.namespace,
                    session_id,
                    json.dumps(
                        {
                            "text": message["content"],
                            "messages": [message],
                            "benchmark_locator": {"session_id": session_id, "turn_index": turn_index},
                        }
                    ),
                    "2023-05-20T02:21:00+00:00",
                    "2023-05-20T02:22:00+00:00",
                    "message",
                    message["role"],
                    f"longmemeval:case-session-window:{session_id}:turn:{turn_index}",
                ),
            )
        retrieved = [
            {
                "rank": 1,
                "text": "The user plans to make onigiri for lunch.",
                "value": "make onigiri for lunch",
                "evidence_event_ids": [event_ids[6]],
            },
            {
                "rank": 2,
                "text": "The user will try shaping onigiri again.",
                "value": "try shaping onigiri again",
                "evidence_event_ids": [event_ids[8]],
            },
            {
                "rank": 3,
                "text": "Onigiri keeps for several days.",
                "value": "keeps for several days",
                "evidence_event_ids": [event_ids[11]],
            },
        ]

        prompt = runner._build_reader_user_prompt(connection, case, retrieved, context_mode="windowed")
        events_json = prompt.split("Original Evidence Events:\n", 1)[1].split("\n\nQuestion:", 1)[0]
        events = json.loads(events_json)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["session_id"], session_id)
        self.assertIn("favorite Japanese short-grain rice", events[0]["content"])
        self.assertIn(4, events[0]["window"]["included_turns"])
        self.assertEqual(events[0]["window"]["total_turns"], len(messages))
        self.assertLessEqual(runner.estimate_tokens(prompt), runner.QA_CONTEXT_TOKEN_BUDGET)
        connection.close()

    def test_reader_turn_radius_uses_numeric_turn_index_gaps(self) -> None:
        content, window = runner._reader_turn_window(
            [
                {"role": "user", "content": "My favorite rice is Japanese short-grain rice."},
                {"role": "assistant", "content": "A later non-adjacent event."},
            ],
            "What type of rice is my favorite?",
            [("Japanese short-grain rice", 1.0)],
            turn_indices=[4, 6],
        )

        self.assertEqual(window["included_turns"], [4])
        self.assertNotIn("later non-adjacent", content)

    def test_reader_needles_fold_obvious_same_session_duplicates(self) -> None:
        folded = reader_context.fold_reader_needles(
            [
                ("Inherited grandmother's vintage diamond necklace", 2.0),
                ("Inherited grandmother's vintage diamond necklace.", 3.0),
                ("The necklace was appraised at $4,000", 2.0),
            ]
        )

        self.assertEqual(len(folded), 2)
        self.assertEqual(folded[0], ("Inherited grandmother's vintage diamond necklace.", 3.0))
        self.assertEqual(folded[1][0], "The necklace was appraised at $4,000")

    def test_matched_user_turn_focuses_question_before_claim_needles(self) -> None:
        content = (
            "What did I inherit from my grandmother? "
            + "opening context " * 90
            + "Inherited grandmother's vintage diamond necklace."
        )

        excerpt, _ = reader_context.reader_turn_window(
            [{"role": "user", "content": content}],
            "What did I inherit from my grandmother?",
            [("Inherited grandmother's vintage diamond necklace", 20.0)],
        )

        self.assertIn("What did I inherit from my grandmother?", excerpt)
        self.assertNotIn("vintage diamond necklace", excerpt)

    def test_reader_excerpt_preserves_sentence_start_before_truncation(self) -> None:
        content = (
            "old context " * 30
            + ". My grandmother's vintage diamond necklace, which had been in the family for three generations "
            + "and was carefully stored in a velvet box, was appraised after I inherited it. "
            + "later context " * 30
        )

        excerpt = reader_context.reader_turn_excerpt(
            content,
            128,
            [("was appraised after I inherited it", 3.0)],
        )

        self.assertIn("My grandmother's vintage diamond necklace", excerpt)
        self.assertNotIn("context . My grandmother", excerpt)

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
            enable_llm_thinking=True,
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
        self.assertEqual(
            [json.loads(request.content)["enable_thinking"] for request in requests],
            [True, True, False],
        )
        self.assertEqual([json.loads(request.content)["temperature"] for request in requests], [0.1, 0.1, 0.0])
        reader_payload = json.loads(requests[1].content)
        self.assertNotIn("response_format", reader_payload)
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
        self.assertEqual(judge_payload["response_format"], {"type": "json_object"})
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

    def test_resume_rejects_report_without_fragment_protocol_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dataset = Path(directory) / "dataset.json"
            dataset.write_text(json.dumps([_record("case-1")]), encoding="utf-8")
            dataset_sha256 = hashlib.sha256(dataset.read_bytes()).hexdigest()
            report = _shard_report(dataset_sha256, [_case_result("case-1")])
            del report["run"]["models"]["extraction_fragment_protocol"]  # type: ignore[index]
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

            with self.assertRaisesRegex(ValueError, "extraction_fragment_protocol"):
                runner._validate_resume_report(report, args, settings)

    def test_resume_rejects_llm_payload_or_query_expansion_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dataset = Path(directory) / "dataset.json"
            dataset.write_text(json.dumps([_record("case-1")]), encoding="utf-8")
            dataset_sha256 = hashlib.sha256(dataset.read_bytes()).hexdigest()
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
            changes = {
                "extractor_effective_provider": "openai_compatible",
                "extractor_base_url": "https://example.com/v1",
                "extractor_structured_mode": "json_schema",
                "extractor_thinking": True,
                "reader_context_protocol": "session-turn-window-v1",
                "query_expansion_model": "different-expander",
            }

            for field, value in changes.items():
                with self.subTest(field=field):
                    report = _shard_report(dataset_sha256, [_case_result("case-1")])
                    report["run"]["models"][field] = value  # type: ignore[index]
                    with self.assertRaisesRegex(ValueError, "model configuration"):
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

    def test_merge_rejects_mixed_fragment_protocols(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset, dataset_sha256 = self._write_dataset(root)
            first = _shard_report(dataset_sha256, [_case_result("case-1")])
            second = _shard_report(dataset_sha256, [_case_result("case-2")])
            second["run"]["models"]["extraction_fragment_protocol"] = "hard-split-v0"  # type: ignore[index]
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
