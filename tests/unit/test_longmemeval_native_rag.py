from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from evaluation.tools import run_longmemeval_benchmark as runner
from evaluation.tools.longmemeval import native_rag
from evaluation.tools.longmemeval.qa_client import QAUsage
from evaluation.tools.run_embedding_ablation import Cost
from hl_mem.settings import Settings


def _case() -> runner.LongMemEvalCase:
    return runner.LongMemEvalCase(
        case_id="native-rag-case",
        question_type="multi-session",
        question="Where did I study?",
        answer="Paris",
        question_at="2024-01-04T12:00:00+00:00",
        sessions=(
            runner.SessionInput(
                session_id="late",
                event_id="event-late",
                occurred_at="2024-01-03T09:00:00+00:00",
                messages=({"role": "assistant", "content": "later answer"},),
            ),
            runner.SessionInput(
                session_id="early",
                event_id="event-early",
                occurred_at="2024-01-01T09:00:00+00:00",
                messages=({"role": "user", "content": "I studied in Paris."},),
            ),
            runner.SessionInput(
                session_id="middle",
                event_id="event-middle",
                occurred_at="2024-01-02T09:00:00+00:00",
                messages=(
                    {"role": "user", "content": "middle question"},
                    {"role": "assistant", "content": "middle answer"},
                ),
            ),
        ),
        gold_event_ids=("event-early",),
        gold_session_ids=("early",),
    )


class RawSessionRetrievalTests(unittest.TestCase):
    def test_documents_preserve_complete_raw_roles_text_and_timestamp(self) -> None:
        documents = native_rag.render_raw_session_documents(_case())

        self.assertEqual([document.session_id for document in documents], ["late", "early", "middle"])
        self.assertIn("Session Date: 2024-01-01T09:00:00+00:00", documents[1].text)
        self.assertIn('"role":"user","content":"I studied in Paris."', documents[1].text)
        self.assertEqual(documents[2].message_count, 2)
        self.assertNotIn("has_answer", "".join(document.text for document in documents))

    def test_exact_cosine_selects_top_k_and_breaks_ties_by_source_order(self) -> None:
        documents = native_rag.render_raw_session_documents(_case())
        vectors = np.asarray(
            [
                [1.0, 0.0],
                [1.0, 0.0],
                [0.0, 1.0],
            ],
            dtype=np.float32,
        )

        hits = native_rag.select_raw_sessions(
            documents,
            vectors,
            np.asarray([1.0, 0.0], dtype=np.float32),
            top_k=2,
        )

        self.assertEqual([hit.document.session_id for hit in hits], ["late", "early"])
        self.assertEqual([hit.retrieval_rank for hit in hits], [1, 2])
        self.assertEqual([hit.score for hit in hits], [1.0, 1.0])

    def test_reader_pack_preserves_selection_but_orders_hits_by_time(self) -> None:
        documents = native_rag.render_raw_session_documents(_case())
        vectors = np.asarray(
            [
                [1.0, 0.0],
                [0.8, 0.2],
                [0.9, 0.1],
            ],
            dtype=np.float32,
        )
        hits = native_rag.select_raw_sessions(
            documents,
            vectors,
            np.asarray([1.0, 0.0], dtype=np.float32),
            top_k=3,
        )

        rendered = native_rag.render_native_rag_user_prompt(_case(), hits)

        self.assertEqual(rendered.retrieval_session_ids, ("late", "middle", "early"))
        self.assertEqual(rendered.reader_session_ids, ("early", "middle", "late"))
        self.assertLess(rendered.prompt.index("I studied in Paris."), rendered.prompt.index("middle question"))
        self.assertLess(rendered.prompt.index("middle question"), rendered.prompt.index("later answer"))
        self.assertTrue(rendered.prompt.endswith("Question: Where did I study?\nAnswer:"))

    def test_selection_validates_top_k_and_vector_shapes(self) -> None:
        documents = native_rag.render_raw_session_documents(_case())
        vectors = np.ones((3, 2), dtype=np.float32)

        with self.assertRaisesRegex(ValueError, "top_k"):
            native_rag.select_raw_sessions(documents, vectors, np.ones(2, dtype=np.float32), top_k=0)
        with self.assertRaisesRegex(ValueError, "document vector count"):
            native_rag.select_raw_sessions(documents, vectors[:2], np.ones(2, dtype=np.float32), top_k=2)
        with self.assertRaisesRegex(ValueError, "dimensions"):
            native_rag.select_raw_sessions(documents, vectors, np.ones(3, dtype=np.float32), top_k=2)


class NativeRagRunnerTests(unittest.TestCase):
    def test_mode_selects_distinct_nativerag_output(self) -> None:
        args = runner.parse_args(["--mode", "native-rag"])

        self.assertEqual(args.mode, "native-rag")
        self.assertEqual(args.output, runner.DEFAULT_NATIVE_RAG_OUTPUT)
        self.assertTrue(args.output.name.startswith("longmemeval_nativerag"))

    def test_control_bypasses_production_and_records_retrieval_trace(self) -> None:
        record = {
            "question_id": "native-control-case",
            "question_type": "multi-session",
            "question": "Where did I study?",
            "answer": "Paris",
            "question_date": "2024/01/04 (Thu) 12:00",
            "answer_session_ids": ["early"],
            "haystack_dates": [
                "2024/01/03 (Wed) 09:00",
                "2024/01/01 (Mon) 09:00",
                "2024/01/02 (Tue) 09:00",
            ],
            "haystack_session_ids": ["late", "early", "middle"],
            "haystack_sessions": [
                [{"role": "assistant", "content": "later answer"}],
                [{"role": "user", "content": "I studied in Paris.", "has_answer": True}],
                [{"role": "user", "content": "middle question"}],
            ],
        }
        settings = Settings(
            llm_api_key="reader-key",
            llm_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            llm_model="deepseek-v4-flash-0731",
            embedding_api_key="embedding-key",
            embedder_mode="real",
            embedding_base_url="https://dashscope.aliyuncs.com",
            embedding_model="qwen3.7-text-embedding",
            embedding_dim=2048,
            embedding_api_mode="native",
            embedding_text_type=None,
        )
        document_cost = Cost(api_calls=1, tokens=300, latency_seconds=0.3, network_api_calls_this_run=1)
        query_cost = Cost(api_calls=1, tokens=10, latency_seconds=0.1, network_api_calls_this_run=1)
        embeddings = [
            SimpleNamespace(
                dense=np.asarray([[1.0, 0.0], [0.95, 0.05], [0.0, 1.0]], dtype=np.float32),
                sparse=None,
                cost=document_cost,
            ),
            SimpleNamespace(
                dense=np.asarray([[1.0, 0.0]], dtype=np.float32),
                sparse=None,
                cost=query_cost,
            ),
        ]
        qa_responses = [
            ("Paris", QAUsage(1_000, 220, 180, 1_220)),
            ('{"correct":true,"reason":"matches"}', QAUsage(100, 20, 0, 120)),
        ]
        client = SimpleNamespace(close=lambda: None)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "native.json"
            output = root / "longmemeval_nativerag_smoke.json"
            dataset.write_text(json.dumps([record]), encoding="utf-8")
            with (
                patch.object(runner, "load_settings", return_value=settings),
                patch.object(
                    runner,
                    "initialize_process",
                    side_effect=AssertionError("production initialization must be bypassed"),
                ),
                patch.object(runner, "DashScopeEmbeddingClient", return_value=client) as client_factory,
                patch.object(runner, "embed_remote", side_effect=embeddings) as embed,
                patch.object(runner, "_qa_dashscope_chat_detailed", side_effect=qa_responses) as chat,
                patch.object(runner, "NATIVE_RAG_CACHE_ROOT", root / "cache"),
            ):
                exit_code = runner.main(
                    [
                        "--mode",
                        "native-rag",
                        "--dataset",
                        str(dataset),
                        "--output",
                        str(output),
                        "--limit",
                        "1",
                    ]
                )

            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["control"], "native-rag")
        self.assertEqual(report["run"]["mode"], "native-rag")
        self.assertEqual(report["run"]["top_k"], 10)
        self.assertEqual(report["run"]["models"]["embedder"], "qwen3.7-text-embedding")
        case = report["cases"][0]
        self.assertIsNone(case["database"])
        self.assertIsNone(case["ingest"])
        self.assertEqual(case["retrieval"]["selector"], "exact-cosine-top-10")
        self.assertEqual(case["retrieval"]["selected_session_ids"][:2], ["late", "early"])
        self.assertEqual(case["retrieval"]["reader_session_ids"], ["early", "middle", "late"])
        self.assertEqual(case["retrieval"]["session_recall_at_5"], 1.0)
        self.assertEqual(case["retrieved"][0]["dense_score"], 1.0)
        self.assertEqual(case["retrieved"][0]["retrieval_rank"], 1)
        self.assertEqual(case["retrieved"][0]["reader_rank"], 3)
        self.assertEqual(case["embedding"]["usage"]["tokens"], 310)
        self.assertEqual(case["qa"]["usage"]["reader_input_tokens"], 1_000)
        self.assertGreater(case["cost"]["embedding_cny"], 0)
        self.assertGreater(case["cost"]["total_cny"], case["cost"]["embedding_cny"])
        self.assertEqual(report["metrics"]["overall"]["session_retrieval_eligible_cases"], 1)
        self.assertEqual(report["metrics"]["overall"]["extraction_coverage_denominator"], 0)
        self.assertEqual(client_factory.call_args.kwargs["timeout_seconds"], 90)
        self.assertEqual(embed.call_count, 2)
        self.assertEqual(embed.call_args_list[0].args[2], "document")
        self.assertEqual(embed.call_args_list[1].args[2], "query")
        self.assertFalse(embed.call_args_list[0].args[1].use_text_type)
        self.assertFalse(embed.call_args_list[0].args[1].use_instruct)
        self.assertEqual(chat.call_args_list[0].kwargs["timeout_seconds"], 300)
        self.assertEqual(chat.call_args_list[0].kwargs["thinking_budget"], 2048)
        self.assertIn("timestamped raw chat sessions", chat.call_args_list[0].args[3])
        self.assertNotIn("long-term-memory claims", chat.call_args_list[0].args[3])

        resume_args = runner.parse_args(
            [
                "--mode",
                "native-rag",
                "--dataset",
                str(dataset),
                "--output",
                str(output),
                "--limit",
                "1",
                "--resume",
            ]
        )
        resume_args.dataset_sha256 = report["dataset"]["sha256"]
        runner._validate_native_rag_resume_report(report, resume_args, settings)
        report["run"]["top_k"] = 5
        with self.assertRaisesRegex(ValueError, "top_k"):
            runner._validate_native_rag_resume_report(report, resume_args, settings)


if __name__ == "__main__":
    unittest.main()
