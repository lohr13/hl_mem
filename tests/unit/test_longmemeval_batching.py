from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

import httpx

from evaluation.tools import merge_longmemeval_results as merger
from evaluation.tools import run_longmemeval_benchmark as runner
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
            self.assertEqual(payload["temperature"], 0.1)
            self.assertEqual(payload["max_tokens"], 512)
            self.assertNotIn("response_format", payload)

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
