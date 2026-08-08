from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from evaluation.tools.run_longmemeval_benchmark import (
    _case_fingerprint,
    _claim_relevance_scores,
    _config_compare_report,
    _longmemeval_recall_intent,
    _reembed_database,
    _sample_stratified,
    _validate_manifest,
    aggregate_results,
    iter_case_records,
    normalize_case,
    retrieval_metrics,
)
from hl_mem.core.vector import pack_vector, unpack_vector
from hl_mem.domain.temporal import RecallIntent
from hl_mem.ingest.llm_extractor import LLM_EXTRACTOR_VERSION
from hl_mem.settings import Settings


def _official_record() -> dict[str, object]:
    return {
        "question_id": "case-1",
        "question_type": "multi-session",
        "question": "What degree did I graduate with?",
        "answer": "Business Administration",
        "question_date": "2023/05/30 (Tue) 23:40",
        "answer_session_ids": ["answer-session"],
        "haystack_dates": [
            "2023/05/20 (Sat) 02:21",
            "2023/05/21 (Sun) 03:24",
        ],
        "haystack_session_ids": ["distractor-session", "answer-session"],
        "haystack_sessions": [
            [{"role": "user", "content": "I like jazz."}],
            [
                {"role": "user", "content": "I studied Business Administration."},
                {"role": "assistant", "content": "Congratulations!"},
            ],
        ],
    }


class LongMemEvalParsingTests(unittest.TestCase):
    def test_normalize_official_record_preserves_session_evidence(self) -> None:
        case = normalize_case(_official_record())

        self.assertEqual(case.case_id, "case-1")
        self.assertEqual(case.question_type, "multi-session")
        self.assertEqual(len(case.sessions), 2)
        self.assertEqual(case.sessions[1].session_id, "answer-session")
        self.assertEqual(case.sessions[1].occurred_at, "2023-05-21T03:24:00+00:00")
        self.assertEqual(len(case.sessions[1].messages), 2)
        self.assertEqual(case.gold_event_ids, (case.sessions[1].event_id,))

    def test_normalize_flat_chat_history_groups_turns_by_session(self) -> None:
        record = {
            "id": "flat-case",
            "type": "single-session-user",
            "question": "Where do I live?",
            "answer": "Paris",
            "evidence": ["dialog-2"],
            "chat_history": [
                {
                    "id": "dialog-1",
                    "role": "user",
                    "content": "Hello",
                    "session_id": "s1",
                    "timestamp": "2024-01-01T10:00:00Z",
                },
                {
                    "id": "dialog-2",
                    "role": "user",
                    "content": "I live in Paris",
                    "session_id": "s2",
                    "timestamp": "2024-01-02T10:00:00Z",
                },
            ],
        }

        case = normalize_case(record)

        self.assertEqual([session.session_id for session in case.sessions], ["s1", "s2"])
        self.assertEqual(case.gold_event_ids, (case.sessions[1].event_id,))

    def test_duplicate_official_session_ids_get_unique_events(self) -> None:
        record = _official_record()
        record["haystack_session_ids"] = ["duplicate", "duplicate"]
        record["answer_session_ids"] = ["duplicate"]

        case = normalize_case(record)

        self.assertEqual([session.session_id for session in case.sessions], ["duplicate", "duplicate#2"])
        self.assertEqual(len(set(session.event_id for session in case.sessions)), 2)
        self.assertEqual(case.gold_event_ids, tuple(session.event_id for session in case.sessions))

    def test_limited_stream_can_read_complete_prefix_of_partial_download(self) -> None:
        first = _official_record()
        partial = json.dumps([first], ensure_ascii=False)[:-1] + ', {"question_id": '
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "partial.json"
            path.write_text(partial, encoding="utf-8")

            records = list(iter_case_records(path, limit=1))

        self.assertEqual(records, [first])

    def test_question_type_selects_recall_intent_without_using_question_date(self) -> None:
        record = _official_record()
        record["question"] = "What was my favorite degree before graduation?"
        current = normalize_case(record)
        record["question_type"] = "single-session-preference"
        preference = normalize_case(record)
        record["question_type"] = "temporal-reasoning"
        temporal = normalize_case(record)

        self.assertIs(_longmemeval_recall_intent(current), RecallIntent.CURRENT_STATE)
        self.assertIs(_longmemeval_recall_intent(preference), RecallIntent.PREFERENCE)
        self.assertIs(_longmemeval_recall_intent(temporal), RecallIntent.HISTORICAL)


class LongMemEvalMetricTests(unittest.TestCase):
    def test_claim_relevance_is_primary_and_session_relevance_is_auxiliary(self) -> None:
        results = [
            {"id": "same-session-noise", "evidence": [{"type": "event", "id": "gold"}]},
            {"id": "answering-claim", "evidence": [{"type": "event", "id": "distractor"}]},
        ]

        metrics = retrieval_metrics(
            results,
            ("gold",),
            relevance_by_claim_id={"same-session-noise": 0.2, "answering-claim": 0.9},
        )

        self.assertTrue(metrics["eligible"])
        self.assertEqual(metrics["recall_at_1"], 0.0)
        self.assertEqual(metrics["recall_at_5"], 1.0)
        self.assertEqual(metrics["mrr"], 0.5)
        self.assertEqual(metrics["first_relevant_rank"], 2)
        self.assertEqual(metrics["session_recall_at_1"], 1.0)
        self.assertEqual(metrics["session_mrr"], 1.0)

    def test_claim_recall_counts_all_relevant_claims_while_hit_stays_binary(self) -> None:
        results = [{"id": "first"}, {"id": "noise"}, {"id": "second"}]

        metrics = retrieval_metrics(
            results,
            (),
            relevance_by_claim_id={"first": 0.9, "noise": 0.1, "second": 0.8},
        )

        self.assertEqual(metrics["recall_at_1"], 0.5)
        self.assertEqual(metrics["hit_at_1"], 1.0)
        self.assertEqual(metrics["recall_at_5"], 1.0)
        self.assertEqual(metrics["hit_at_5"], 1.0)

    def test_claim_metrics_are_ineligible_without_relevant_extracted_claims(self) -> None:
        metrics = retrieval_metrics(
            [{"id": "noise"}],
            (),
            relevance_by_claim_id={"noise": 0.1},
        )

        self.assertFalse(metrics["eligible"])
        self.assertIsNone(metrics["recall_at_1"])
        self.assertIsNone(metrics["hit_at_1"])

    def test_claim_relevance_scores_use_answer_similarity(self) -> None:
        class FakeEmbedder:
            vectors = {
                "The user lives in Paris": pack_vector([1.0, 0.0]),
                "The user likes jazz": pack_vector([0.0, 1.0]),
                "Paris": pack_vector([1.0, 0.0]),
            }

            def embed_batch(self, texts: list[str]) -> list[bytes]:
                return [self.vectors[text] for text in texts]

        scores = _claim_relevance_scores(
            {"place": "The user lives in Paris", "music": "The user likes jazz"},
            "Paris",
            FakeEmbedder(),
        )

        self.assertAlmostEqual(scores["place"], 1.0)
        self.assertAlmostEqual(scores["music"], 0.0)

    def test_aggregate_excludes_abstention_from_retrieval_denominator(self) -> None:
        cases = [
            {
                "question_type": "single-session-user",
                "retrieval": {
                    "eligible": True,
                    "recall_at_1": 1.0,
                    "recall_at_5": 1.0,
                    "recall_at_10": 1.0,
                    "mrr": 1.0,
                },
                "qa": {"correct": True},
                "error": None,
            },
            {
                "question_type": "abstention",
                "retrieval": {
                    "eligible": False,
                    "recall_at_1": None,
                    "recall_at_5": None,
                    "recall_at_10": None,
                    "mrr": None,
                },
                "qa": {"correct": False},
                "error": None,
            },
        ]

        summary = aggregate_results(cases)

        self.assertEqual(summary["overall"]["cases"], 2)
        self.assertEqual(summary["overall"]["retrieval_eligible_cases"], 1)
        self.assertEqual(summary["overall"]["recall_at_10"], 1.0)
        self.assertEqual(summary["overall"]["qa_accuracy"], 0.5)
        self.assertIn("abstention", summary["by_type"])


class LongMemEvalConfigCompareTests(unittest.TestCase):
    def test_manifest_rejects_embedding_api_or_text_type_changes(self) -> None:
        case = normalize_case(_official_record())
        settings = Settings(
            embedding_model="qwen3.7-text-embedding",
            embedding_dim=2048,
            embedding_api_mode="native",
            embedding_text_type=None,
        )
        manifest = {
            "case_id": case.case_id,
            "case_fingerprint": "will-be-replaced",
            "session_count": len(case.sessions),
            "extractor_version": LLM_EXTRACTOR_VERSION,
            "embedding_model": settings.embedding_model,
            "embedding_dim": settings.embedding_dim,
            "embedding_api_mode": "compatible",
            "embedding_text_type": "document",
        }
        manifest["case_fingerprint"] = _case_fingerprint(case)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "embedding_api_mode"):
                _validate_manifest(path, case, settings)

    def test_config_compare_report_uses_fixed_relevance_scorer_metadata(self) -> None:
        case = normalize_case(_official_record())
        settings = Settings()
        with tempfile.TemporaryDirectory() as directory:
            dataset = Path(directory) / "dataset.json"
            dataset.write_text("[]", encoding="utf-8")
            args = SimpleNamespace(
                dataset=dataset,
                skip_ingest=False,
                no_qa=True,
                clean=False,
            )

            report = _config_compare_report(args, settings, [case], [], {}, "start", "running")

        scorer = report["run"]["relevance_scorer"]
        self.assertEqual(scorer["code"], "V0")
        self.assertEqual(scorer["model"], "text-embedding-v4")
        self.assertEqual(scorer["api"], "compatible")
        self.assertIsNone(scorer["text_type"])

    def test_stratified_sample_takes_first_two_of_each_type(self) -> None:
        records = [
            {"question_type": question_type, "question_id": f"{question_type}-{index}"}
            for index in range(3)
            for question_type in (
                "single-session-user",
                "single-session-assistant",
                "single-session-preference",
                "multi-session",
                "knowledge-update",
                "temporal-reasoning",
            )
        ]

        sampled = _sample_stratified(records, n_per_type=2)

        self.assertEqual(len(sampled), 12)
        by_type: dict[str, list[str]] = {}
        for record in sampled:
            by_type.setdefault(str(record["question_type"]), []).append(str(record["question_id"]))
        self.assertTrue(all(len(items) == 2 for items in by_type.values()))
        self.assertEqual(
            by_type["single-session-user"],
            ["single-session-user-0", "single-session-user-1"],
        )

    def test_reembed_database_replaces_dense_vectors_without_sparse_storage(self) -> None:
        class FakeVariantEmbedder:
            model = "fake-model"
            dim = 2
            sparse_requested = True

            def embed_documents(self, texts: list[str]) -> list[bytes]:
                self.seen = texts
                return [pack_vector([float(index + 1), 0.0]) for index in range(len(texts))]

            def cost_snapshot(self) -> dict[str, int]:
                return {"api_calls": 1}

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "case.db"
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE claims ("
                "id TEXT PRIMARY KEY,index_text TEXT,embedding_dense BLOB,"
                "embedding_sparse BLOB,embedding_model TEXT,embedding_dim INTEGER)"
            )
            connection.executemany(
                "INSERT INTO claims(id,index_text,embedding_dense,embedding_sparse) VALUES(?,?,?,?)",
                [
                    ("c1", "first", pack_vector([9.0, 9.0]), b"old"),
                    ("c2", "second", pack_vector([9.0, 9.0]), b"old"),
                ],
            )
            connection.commit()
            connection.close()
            embedder = FakeVariantEmbedder()

            stats = _reembed_database(path, {"output_type": "sparse"}, embedder)

            connection = sqlite3.connect(path)
            rows = connection.execute(
                "SELECT embedding_dense,embedding_sparse,embedding_model,embedding_dim " "FROM claims ORDER BY id"
            ).fetchall()
            connection.close()

        self.assertEqual(embedder.seen, ["first", "second"])
        self.assertEqual(unpack_vector(rows[0][0]), (1.0, 0.0))
        self.assertEqual(unpack_vector(rows[1][0]), (2.0, 0.0))
        self.assertTrue(all(row[1] is None for row in rows))
        self.assertTrue(all(row[2:] == ("fake-model", 2) for row in rows))
        self.assertEqual(stats["claims_reembedded"], 2)
        self.assertEqual(stats["sparse_mode"], "dense_only")


if __name__ == "__main__":
    unittest.main()
