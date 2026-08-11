from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from evaluation.tools import run_longmemeval_benchmark as runner
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
from hl_mem.config_loader import load_settings
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
        self.assertEqual(
            case.gold_event_ids,
            tuple(runner._turn_event_id(case.sessions[1].event_id, index) for index in range(2)),
        )

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
        self.assertEqual(case.gold_event_ids, (runner._turn_event_id(case.sessions[1].event_id, 0),))

    def test_duplicate_official_session_ids_get_unique_events(self) -> None:
        record = _official_record()
        record["haystack_session_ids"] = ["duplicate", "duplicate"]
        record["answer_session_ids"] = ["duplicate"]

        case = normalize_case(record)

        self.assertEqual([session.session_id for session in case.sessions], ["duplicate", "duplicate#2"])
        self.assertEqual(len(set(session.event_id for session in case.sessions)), 2)
        self.assertEqual(
            case.gold_event_ids,
            tuple(
                runner._turn_event_id(session.event_id, turn_index)
                for session in case.sessions
                for turn_index in range(len(session.messages))
            ),
        )

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


class LongMemEvalModelConfigurationTests(unittest.TestCase):
    @staticmethod
    def _production_settings(**overrides: object) -> Settings:
        values: dict[str, object] = {
            "llm_api_key": "llm-key",
            "llm_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "llm_model": "deepseek-v4-flash",
            "llm_provider": "openai_compatible",
            "llm_structured_mode": "json_object",
            "enable_llm_thinking": False,
            "embedder_mode": "real",
            "embedding_api_key": "embedding-key",
            "embedding_model": "qwen3.7-text-embedding",
            "embedding_dim": 2048,
            "embedding_api_mode": "native",
        }
        values.update(overrides)
        return Settings(**values)

    def test_production_gate_accepts_configured_deepseek_and_arbitrary_models(self) -> None:
        for model in ("deepseek-v4-flash", "custom-evaluation-model"):
            with self.subTest(model=model):
                runner._validate_production_settings(self._production_settings(llm_model=model))

    def test_production_gate_requires_json_object_for_deepseek(self) -> None:
        for mode in ("auto", "json_schema"):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(ValueError, "json_object"):
                    runner._validate_production_settings(self._production_settings(llm_structured_mode=mode))

    def test_bailian_openai_compatible_components_get_explicit_thinking_control(self) -> None:
        settings = self._production_settings()

        component_settings = runner._component_llm_settings(settings)

        self.assertEqual(settings.llm_provider, "openai_compatible")
        self.assertEqual(component_settings.llm_provider, "dashscope")
        self.assertFalse(component_settings.enable_llm_thinking)

    def test_non_bailian_compatible_endpoint_keeps_generic_provider(self) -> None:
        settings = self._production_settings(llm_base_url="https://dashscope-proxy.example.com/compatible-mode/v1")

        component_settings = runner._component_llm_settings(settings)

        self.assertIs(component_settings, settings)
        self.assertEqual(component_settings.llm_provider, "openai_compatible")

    def test_qa_model_uses_configured_model_with_arbitrary_environment_override(self) -> None:
        settings = self._production_settings()
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(runner._qa_model(settings), "deepseek-v4-flash")
        with patch.dict(os.environ, {"HL_MEM_EVAL_QA_MODEL": "reader-model-v9"}, clear=True):
            self.assertEqual(runner._qa_model(settings), "reader-model-v9")

    def test_deepseek_example_config_is_benchmark_ready(self) -> None:
        config_path = runner.ROOT / "evaluation" / "tools" / "configs" / "longmemeval_deepseek_v4_flash.toml"

        settings = load_settings(
            config_path,
            runner.ROOT / ".env-not-used",
            environ={
                "LLM_API_KEY": "llm-key",
                "EMBEDDING_API_KEY": "embedding-key",
                "RERANKER_API_KEY": "reranker-key",
            },
        )

        self.assertEqual(settings.llm_provider, "openai_compatible")
        self.assertEqual(settings.llm_base_url, "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.assertEqual(settings.llm_model, "deepseek-v4-flash-0731")
        self.assertEqual(settings.llm_structured_mode, "json_object")
        self.assertFalse(settings.enable_llm_thinking)
        self.assertEqual(settings.query_expansion_model, "deepseek-v4-flash")
        runner._validate_production_settings(settings)


class LongMemEvalMetricTests(unittest.TestCase):
    def test_temporal_gate_marks_same_day_future_gold_sessions_ambiguous(self) -> None:
        record = _official_record()
        record["question_id"] = "gpt4_2f56ae70"
        record["question_type"] = "temporal-reasoning"
        record["question_date"] = "2023/05/26 (Fri) 00:18"
        record["answer_session_ids"] = ["gold-a", "gold-b"]
        record["haystack_session_ids"] = ["gold-a", "gold-b"]
        record["haystack_dates"] = ["2023/05/26 (Fri) 01:08", "2023/05/26 (Fri) 08:25"]
        record["haystack_sessions"] = [
            [{"role": "user", "content": "Started a trial."}],
            [{"role": "user", "content": "Discussed the trial."}],
        ]
        ambiguous = normalize_case(record)

        eligibility = runner._evaluation_eligibility(ambiguous)

        self.assertEqual(eligibility["status"], "invalid_ambiguous")
        self.assertFalse(eligibility["temporal_gate_eligible"])
        self.assertEqual(eligibility["reason_code"], "gold_sessions_after_question_same_day")

        record["question_id"] = "valid-temporal"
        record["haystack_dates"] = ["2023/05/25 (Thu) 01:08", "2023/05/26 (Fri) 00:10"]
        valid = normalize_case(record)
        self.assertTrue(runner._evaluation_eligibility(valid)["temporal_gate_eligible"])

    def test_temporal_gate_keeps_mixed_past_and_future_gold_sessions(self) -> None:
        record = _official_record()
        record["question_type"] = "temporal-reasoning"
        record["question_date"] = "2023/05/26 (Fri) 12:00"
        record["answer_session_ids"] = ["gold-before", "gold-after"]
        record["haystack_session_ids"] = ["gold-before", "gold-after"]
        record["haystack_dates"] = ["2023/05/26 (Fri) 11:59", "2023/05/26 (Fri) 12:01"]
        record["haystack_sessions"] = [
            [{"role": "user", "content": "Earlier evidence."}],
            [{"role": "user", "content": "Later evidence."}],
        ]

        eligibility = runner._evaluation_eligibility(normalize_case(record))

        self.assertEqual(eligibility["status"], "eligible")
        self.assertTrue(eligibility["temporal_gate_eligible"])

    def test_temporal_gate_keeps_future_gold_for_non_temporal_question(self) -> None:
        record = _official_record()
        record["question_type"] = "multi-session"
        record["question_date"] = "2023/05/26 (Fri) 12:00"
        record["haystack_dates"] = ["2023/05/26 (Fri) 12:01", "2023/05/26 (Fri) 12:02"]

        eligibility = runner._evaluation_eligibility(normalize_case(record))

        self.assertEqual(eligibility["status"], "eligible")
        self.assertTrue(eligibility["temporal_gate_eligible"])

    def test_temporal_gate_keeps_cross_day_future_gold(self) -> None:
        record = _official_record()
        record["question_type"] = "temporal-reasoning"
        record["question_date"] = "2023/05/26 (Fri) 23:59"
        record["haystack_dates"] = ["2023/05/27 (Sat) 00:01", "2023/05/27 (Sat) 00:02"]

        eligibility = runner._evaluation_eligibility(normalize_case(record))

        self.assertEqual(eligibility["status"], "eligible")
        self.assertTrue(eligibility["temporal_gate_eligible"])

    def test_temporal_gate_compares_same_day_in_question_timezone(self) -> None:
        case = normalize_case(_official_record())
        case = dataclasses.replace(
            case,
            question_type="temporal-reasoning",
            question_at="2023-05-26T23:30:00-02:00",
            sessions=tuple(
                (
                    dataclasses.replace(session, occurred_at="2023-05-27T01:45:00+00:00")
                    if session.session_id in case.gold_session_ids
                    else session
                )
                for session in case.sessions
            ),
        )

        eligibility = runner._evaluation_eligibility(case)

        self.assertEqual(eligibility["status"], "invalid_ambiguous")
        self.assertFalse(eligibility["temporal_gate_eligible"])

    @unittest.skipUnless(
        (runner.ROOT / "evaluation" / "datasets" / "holdout50_all.json").is_file(),
        "requires the local holdout50 dataset",
    )
    def test_holdout50_temporal_gate_excludes_the_reviewed_dataset_ids(self) -> None:
        dataset = runner.ROOT / "evaluation" / "datasets" / "holdout50_all.json"
        excluded: set[str] = set()
        for record in iter_case_records(dataset):
            case = normalize_case(record)
            if runner._evaluation_eligibility(case)["temporal_gate_eligible"] is False:
                excluded.add(case.case_id)

        self.assertEqual(excluded, {"gpt4_2f56ae70", "gpt4_5dcc0aab"})

    def test_reviewed_temporal_ids_are_excluded_without_external_dataset_fixture(self) -> None:
        reviewed_dates = {
            "gpt4_5dcc0aab": (
                "2023/05/24 (Wed) 09:14",
                (
                    "2023/05/24 (Wed) 18:32",
                    "2023/05/24 (Wed) 10:58",
                    "2023/05/24 (Wed) 14:49",
                    "2023/05/24 (Wed) 20:15",
                    "2023/05/24 (Wed) 19:37",
                ),
            ),
            "gpt4_2f56ae70": (
                "2023/05/26 (Fri) 00:18",
                (
                    "2023/05/26 (Fri) 23:40",
                    "2023/05/26 (Fri) 01:08",
                    "2023/05/26 (Fri) 08:25",
                ),
            ),
        }
        excluded: set[str] = set()
        for case_id, (question_date, gold_dates) in reviewed_dates.items():
            record = _official_record()
            gold_ids = [f"gold-{index}" for index in range(len(gold_dates))]
            record.update(
                {
                    "question_id": case_id,
                    "question_type": "temporal-reasoning",
                    "question_date": question_date,
                    "answer_session_ids": gold_ids,
                    "haystack_session_ids": gold_ids,
                    "haystack_dates": list(gold_dates),
                    "haystack_sessions": [
                        [{"role": "user", "content": f"gold evidence {index}"}] for index in range(len(gold_dates))
                    ],
                }
            )
            case = normalize_case(record)
            if runner._evaluation_eligibility(case)["temporal_gate_eligible"] is False:
                excluded.add(case.case_id)

        self.assertEqual(excluded, {"gpt4_2f56ae70", "gpt4_5dcc0aab"})

    def test_claim_relevance_is_primary_and_session_relevance_is_auxiliary(self) -> None:
        results = [
            {"id": "same-session-noise", "evidence": [{"type": "event", "id": "gold"}]},
            {"id": "answering-claim", "evidence": [{"type": "event", "id": "distractor"}]},
        ]

        metrics = retrieval_metrics(
            results,
            ("gold",),
            relevance_by_claim_id={"same-session-noise": 0.2, "answering-claim": 0.9},
            gold_session_ids=("gold-session",),
            event_to_session={"gold": "gold-session", "distractor": "other-session"},
        )

        self.assertTrue(metrics["eligible"])
        self.assertEqual(metrics["recall_at_1"], 0.0)
        self.assertEqual(metrics["recall_at_5"], 1.0)
        self.assertEqual(metrics["mrr"], 0.5)
        self.assertEqual(metrics["first_relevant_rank"], 2)
        self.assertEqual(metrics["session_recall_at_1"], 1.0)
        self.assertEqual(metrics["session_mrr"], 1.0)

    def test_session_recall_counts_sessions_instead_of_turn_events(self) -> None:
        metrics = retrieval_metrics(
            [{"id": "claim", "evidence": [{"type": "event", "id": "gold-turn-2"}]}],
            ("gold-turn-1", "gold-turn-2"),
            relevance_by_claim_id={"claim": 0.9},
            gold_session_ids=("gold-session",),
            event_to_session={
                "gold-turn-1": "gold-session",
                "gold-turn-2": "gold-session",
            },
        )

        self.assertEqual(metrics["session_recall_at_1"], 1.0)
        self.assertEqual(metrics["session_hit_at_1"], 1.0)

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

    def test_aggregate_uses_independent_coverage_claim_and_session_denominators(self) -> None:
        cases = [
            {
                "question_type": "single-session-user",
                "retrieval": {
                    "eligible": True,
                    "session_eligible": False,
                    "answer_covered_by_extracted_claims": True,
                    "recall_at_1": 1.0,
                    "recall_at_5": 1.0,
                    "recall_at_10": 1.0,
                    "mrr": 1.0,
                    "session_recall_at_1": None,
                    "session_recall_at_5": None,
                    "session_recall_at_10": None,
                    "session_hit_at_1": None,
                    "session_hit_at_5": None,
                    "session_hit_at_10": None,
                    "session_mrr": None,
                },
                "qa": {"correct": True},
                "error": None,
            },
            {
                "question_type": "abstention",
                "retrieval": {
                    "eligible": False,
                    "session_eligible": True,
                    "answer_covered_by_extracted_claims": False,
                    "recall_at_1": None,
                    "recall_at_5": None,
                    "recall_at_10": None,
                    "mrr": None,
                    "session_recall_at_1": 0.5,
                    "session_recall_at_5": 1.0,
                    "session_recall_at_10": 1.0,
                    "session_hit_at_1": 1.0,
                    "session_hit_at_5": 1.0,
                    "session_hit_at_10": 1.0,
                    "session_mrr": 0.5,
                },
                "qa": {"correct": False},
                "error": None,
            },
        ]

        summary = aggregate_results(cases)

        self.assertEqual(summary["overall"]["cases"], 2)
        self.assertEqual(summary["overall"]["retrieval_eligible_cases"], 1)
        self.assertEqual(summary["overall"]["retrieval_eligible_numerator"], 1)
        self.assertEqual(summary["overall"]["retrieval_eligible_denominator"], 2)
        self.assertEqual(summary["overall"]["recall_at_10"], 1.0)
        self.assertEqual(summary["overall"]["recall_at_10_eligible_numerator"], 1)
        self.assertEqual(summary["overall"]["recall_at_10_eligible_denominator"], 2)
        self.assertEqual(summary["overall"]["extraction_coverage_numerator"], 1)
        self.assertEqual(summary["overall"]["extraction_coverage_denominator"], 2)
        self.assertEqual(summary["overall"]["answer_covered_by_extracted_claims"], 0.5)
        self.assertEqual(summary["overall"]["session_retrieval_eligible_numerator"], 1)
        self.assertEqual(summary["overall"]["session_retrieval_eligible_denominator"], 2)
        self.assertEqual(summary["overall"]["session_recall_at_1"], 0.5)
        self.assertEqual(summary["overall"]["qa_accuracy"], 0.5)
        self.assertIn("abstention", summary["by_type"])

    def test_aggregate_reports_gate_accuracy_without_ambiguous_temporal_case(self) -> None:
        cases = [
            {
                "case_id": "eligible",
                "question_type": "temporal-reasoning",
                "retrieval": {
                    "eligible": True,
                    "session_eligible": True,
                    "recall_at_1": 1.0,
                    "session_recall_at_1": 1.0,
                    "mrr": 1.0,
                    "session_mrr": 1.0,
                },
                "qa": {"correct": True},
                "error": None,
                "evaluation_eligibility": {"status": "eligible", "temporal_gate_eligible": True},
            },
            {
                "case_id": "ambiguous",
                "question_type": "temporal-reasoning",
                "retrieval": {
                    "eligible": True,
                    "session_eligible": True,
                    "recall_at_1": 0.0,
                    "session_recall_at_1": 0.0,
                    "mrr": 0.0,
                    "session_mrr": 0.0,
                },
                "qa": {"correct": False},
                "error": None,
                "evaluation_eligibility": {
                    "status": "invalid_ambiguous",
                    "temporal_gate_eligible": False,
                    "reason_code": "gold_sessions_after_question_same_day",
                },
            },
        ]

        temporal = aggregate_results(cases)["by_type"]["temporal-reasoning"]

        self.assertEqual(temporal["qa_accuracy"], 0.5)
        self.assertEqual(temporal["gate_eligible_cases"], 1)
        self.assertEqual(temporal["gate_excluded_cases"], 1)
        self.assertEqual(temporal["gate_excluded_case_ids"], ["ambiguous"])
        self.assertEqual(temporal["gate_qa_accuracy"], 1.0)
        self.assertEqual(temporal["recall_at_1"], 0.5)
        self.assertEqual(temporal["session_recall_at_1"], 0.5)
        self.assertEqual(temporal["gate_recall_at_1"], 1.0)
        self.assertEqual(temporal["gate_session_recall_at_1"], 1.0)
        self.assertEqual(temporal["gate_mrr"], 1.0)
        self.assertEqual(temporal["gate_session_mrr"], 1.0)

    def test_aggregate_treats_legacy_resume_case_without_eligibility_as_gate_eligible(self) -> None:
        legacy = {
            "case_id": "legacy",
            "question_type": "temporal-reasoning",
            "retrieval": {"eligible": True, "session_eligible": False, "recall_at_1": 1.0, "mrr": 1.0},
            "qa": {"correct": True},
            "error": None,
        }

        temporal = aggregate_results([legacy])["by_type"]["temporal-reasoning"]

        self.assertEqual(temporal["gate_eligible_cases"], 1)
        self.assertEqual(temporal["gate_excluded_cases"], 0)
        self.assertEqual(temporal["gate_qa_accuracy"], 1.0)

    def test_claim_inflation_uses_physical_claim_rows_instead_of_write_results(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            "CREATE TABLE claims ("
            "id TEXT PRIMARY KEY,subject_entity_id TEXT,canonical_attribute TEXT,index_text TEXT,status TEXT);"
            "CREATE TABLE evidence_links ("
            "derived_type TEXT,derived_id TEXT,evidence_type TEXT,evidence_id TEXT);"
            "CREATE TABLE events (id TEXT PRIMARY KEY,session_id TEXT,content_json TEXT);"
        )
        connection.executemany(
            "INSERT INTO events VALUES(?,?,?)",
            [
                ("e0", "s1", json.dumps({"benchmark_locator": {"session_id": "s1", "turn_index": 0}})),
                ("e1", "s1", json.dumps({"benchmark_locator": {"session_id": "s1", "turn_index": 1}})),
                ("e5", "s1", json.dumps({"benchmark_locator": {"session_id": "s1", "turn_index": 5}})),
                ("other", "s2", json.dumps({"benchmark_locator": {"session_id": "s2", "turn_index": 1}})),
            ],
        )
        connection.executemany(
            "INSERT INTO claims VALUES(?,?,?,?,?)",
            [
                ("c0", "user", "preference.food", "user prefers Japanese short-grain rice", "active"),
                ("c1", "user", "preference.food", "the user prefers Japanese short grain rice", "active"),
                ("c5", "user", "preference.food", "user prefers Japanese short-grain rice", "superseded"),
                ("c-other", "user", "preference.food", "user prefers Japanese short-grain rice", "retracted"),
            ],
        )
        connection.executemany(
            "INSERT INTO evidence_links VALUES('claim',?,'event',?)",
            [("c0", "e0"), ("c1", "e1"), ("c5", "e5"), ("c-other", "other")],
        )

        diagnostics = runner._claim_inflation_diagnostics(
            connection,
            {"events": 4, "sessions": 2, "extracted_claims": 8, "accepted_claim_writes": 99},
        )

        self.assertEqual(diagnostics["stored_claims"], 4)
        self.assertEqual(diagnostics["extracted_claims_per_event"], 2.0)
        self.assertEqual(diagnostics["stored_claims_per_event"], 1.0)
        self.assertEqual(diagnostics["stored_claims_per_session"], 2.0)
        self.assertEqual(diagnostics["adjacent_restatement_candidates"], 1)
        self.assertIn("diagnostic lexical threshold", diagnostics["adjacent_restatement_definition"])
        connection.close()

    def test_claim_inflation_deduplicates_links_and_ignores_one_claim_across_turns(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            "CREATE TABLE claims ("
            "id TEXT PRIMARY KEY,subject_entity_id TEXT,canonical_attribute TEXT,index_text TEXT,status TEXT);"
            "CREATE TABLE evidence_links ("
            "derived_type TEXT,derived_id TEXT,evidence_type TEXT,evidence_id TEXT);"
            "CREATE TABLE events (id TEXT PRIMARY KEY,session_id TEXT,content_json TEXT);"
        )
        connection.executemany(
            "INSERT INTO events VALUES(?,?,?)",
            [
                ("e0", "s1", json.dumps({"benchmark_locator": {"session_id": "s1", "turn_index": 0}})),
                ("e1", "s1", json.dumps({"benchmark_locator": {"session_id": "s1", "turn_index": 1}})),
            ],
        )
        connection.execute(
            "INSERT INTO claims VALUES(?,?,?,?,?)",
            ("shared", "user", "preference.food", "user prefers rice", "active"),
        )
        connection.executemany(
            "INSERT INTO evidence_links VALUES('claim','shared','event',?)",
            [("e0",), ("e0",), ("e1",)],
        )

        diagnostics = runner._claim_inflation_diagnostics(connection, {"events": 2, "sessions": 1})

        self.assertEqual(diagnostics["stored_claims"], 1)
        self.assertEqual(diagnostics["adjacent_restatement_candidates"], 0)
        connection.close()

    def test_claim_inflation_excludes_terminal_statuses_from_restatement_candidates(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            "CREATE TABLE claims ("
            "id TEXT PRIMARY KEY,subject_entity_id TEXT,canonical_attribute TEXT,index_text TEXT,status TEXT);"
            "CREATE TABLE evidence_links ("
            "derived_type TEXT,derived_id TEXT,evidence_type TEXT,evidence_id TEXT);"
            "CREATE TABLE events (id TEXT PRIMARY KEY,session_id TEXT,content_json TEXT);"
            'INSERT INTO events VALUES(\'e0\',\'s1\',\'{"benchmark_locator":{"session_id":"s1","turn_index":0}}\');'
            'INSERT INTO events VALUES(\'e1\',\'s1\',\'{"benchmark_locator":{"session_id":"s1","turn_index":1}}\');'
        )
        for index, status in enumerate(("superseded", "expired", "archived", "retracted")):
            left = f"{status}-left"
            right = f"{status}-right"
            connection.executemany(
                "INSERT INTO claims VALUES(?,?,?,?,?)",
                [
                    (left, "user", "preference.food", f"user prefers rice {index}", status),
                    (right, "user", "preference.food", f"user prefers rice {index}", status),
                ],
            )
            connection.executemany(
                "INSERT INTO evidence_links VALUES('claim',?,'event',?)",
                [(left, "e0"), (right, "e1")],
            )
        for status in ("candidate", "disputed"):
            left = f"{status}-left"
            right = f"{status}-right"
            connection.executemany(
                "INSERT INTO claims VALUES(?,?,?,?,?)",
                [
                    (left, "user", "preference.food", f"{status} user prefers rice", status),
                    (right, "user", "preference.food", f"{status} user prefers rice", status),
                ],
            )
            connection.executemany(
                "INSERT INTO evidence_links VALUES('claim',?,'event',?)",
                [(left, "e0"), (right, "e1")],
            )

        diagnostics = runner._claim_inflation_diagnostics(connection, {"events": 2, "sessions": 1})

        self.assertEqual(diagnostics["stored_claims"], 12)
        self.assertEqual(diagnostics["adjacent_restatement_candidates"], 2)
        connection.close()

    def test_cached_ingest_computes_stored_density_and_marks_extracted_density_unavailable(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            "CREATE TABLE claims ("
            "id TEXT PRIMARY KEY,subject_entity_id TEXT,canonical_attribute TEXT,index_text TEXT,status TEXT);"
            "CREATE TABLE evidence_links ("
            "derived_type TEXT,derived_id TEXT,evidence_type TEXT,evidence_id TEXT);"
            "CREATE TABLE events (id TEXT PRIMARY KEY,session_id TEXT,content_json TEXT);"
            "INSERT INTO claims VALUES('c0','user','preference.food','user prefers rice','active');"
        )
        case = normalize_case(_official_record())

        cached = runner._cached_ingest_diagnostics(connection, case, "manifest.json")

        self.assertTrue(cached["skipped"])
        self.assertEqual(cached["claim_inflation_diagnostics_status"], "computed_from_cache")
        self.assertIsNone(cached["extracted_claims_per_event"])
        self.assertEqual(cached["stored_claims"], 1)
        self.assertEqual(cached["stored_claims_per_event"], 0.333333)
        connection.close()


class LongMemEvalExtractionFragmentTests(unittest.TestCase):
    def test_fragments_prefer_semantic_boundaries_and_round_trip_nested_unicode_content(self) -> None:
        source = (
            r"第一段包含“引号”和路径 C:\Users\示例。第二句仍在这里。"
            "\n\nSecond paragraph has a complete sentence. Another sentence follows. "
        ) * 8
        alternate = ("Alternate text is independently preserved. It also ends on sentences. ") * 7
        content = {
            "text": source,
            "messages": [
                {
                    "role": "user",
                    "content": source,
                    "text": alternate,
                    "metadata": {"nested": {"tags": ["unicode-中文", 'quote-"', "slash-\\"]}},
                }
            ],
            "benchmark_locator": {"session_id": "session-1", "turn_index": 2},
        }

        fragments = runner.fragment_turn_content(
            content,
            target_chars=700,
            previous_turns=[
                {"role": "user", "content": "old-0"},
                {"role": "assistant", "content": "old-1"},
            ],
            overlap_turns=1,
        )

        self.assertGreater(len(fragments), 1)
        self.assertTrue(all(len(json.dumps(item.content, ensure_ascii=False)) <= 700 for item in fragments))
        self.assertEqual("".join(item.content["text"] for item in fragments), source)
        self.assertEqual("".join(item.content["messages"][0]["content"] for item in fragments), source)
        self.assertEqual("".join(item.content["messages"][0]["text"] for item in fragments), alternate)
        self.assertTrue(
            all(item.content["messages"][0]["metadata"] == content["messages"][0]["metadata"] for item in fragments)
        )
        self.assertEqual(
            {field for item in fragments for field in item.continuity["fragment_source_fields"]},
            {"messages[0].content", "messages[0].text", "text"},
        )
        self.assertTrue(
            all(
                (item.content["messages"][0]["content"] or item.content["messages"][0]["text"]).endswith(
                    ("。", "。\n\n", ". ", ".\n\n")
                )
                for item in fragments[:-1]
            )
        )
        for index, item in enumerate(fragments):
            self.assertEqual(json.loads(json.dumps(item.content, ensure_ascii=False)), item.content)
            self.assertEqual(item.continuity["fragment_index"], index)
            self.assertEqual(item.continuity["total_fragments"], len(fragments))
            self.assertEqual(
                item.continuity["context_only_previous_turns"],
                [{"role": "assistant", "content": "old-1"}],
            )

    def test_oversized_non_text_envelope_remains_one_lossless_json_fragment(self) -> None:
        content = {
            "messages": [{"role": "tool", "payload": {"blob": ["x" * 200, {"deep": "y" * 200}]}}],
            "benchmark_locator": {"session_id": "session-1", "turn_index": 0},
        }

        fragments = runner.fragment_turn_content(content, target_chars=100)

        self.assertEqual(len(fragments), 1)
        self.assertEqual(fragments[0].content, content)
        self.assertTrue(fragments[0].continuity["oversized_envelope"])
        self.assertEqual(json.loads(json.dumps(fragments[0].content)), content)


class LongMemEvalConfigCompareTests(unittest.TestCase):
    def test_manifest_rejects_legacy_session_as_user_event_model(self) -> None:
        case = normalize_case(_official_record())
        settings = Settings()
        manifest = runner._manifest_identity(case, settings)
        manifest["event_model_version"] = "session-as-user-v1"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "event_model_version"):
                _validate_manifest(path, case, settings)

    def test_manifest_rejects_cache_without_fragment_protocol_identity(self) -> None:
        case = normalize_case(_official_record())
        settings = Settings()
        manifest = runner._manifest_identity(case, settings)
        del manifest["extraction_fragment_protocol"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "extraction_fragment_protocol"):
                _validate_manifest(path, case, settings)

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

    def test_manifest_rejects_each_extractor_payload_change(self) -> None:
        case = normalize_case(_official_record())
        settings = Settings(
            llm_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            llm_model="deepseek-v4-flash",
            llm_provider="openai_compatible",
            llm_structured_mode="json_object",
            enable_llm_thinking=False,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            changes = {
                "extractor_model": "different-model",
                "extractor_provider": "dashscope",
                "extractor_effective_provider": "openai_compatible",
                "extractor_base_url": "https://example.com/v1",
                "extractor_structured_mode": "json_schema",
                "extractor_thinking": True,
            }
            for field, value in changes.items():
                with self.subTest(field=field):
                    manifest = runner._manifest_identity(case, settings)
                    manifest[field] = value
                    path.write_text(json.dumps(manifest), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, field):
                        _validate_manifest(path, case, settings)

    def test_manifest_records_endpoint_and_effective_provider(self) -> None:
        case = normalize_case(_official_record())
        settings = Settings(
            llm_base_url="https://DASHSCOPE.ALIYUNCS.COM/compatible-mode/v1/",
            llm_model="deepseek-v4-flash",
            llm_provider="openai_compatible",
            llm_structured_mode="json_object",
            enable_llm_thinking=False,
        )

        identity = runner._manifest_identity(case, settings)

        self.assertEqual(
            identity["extractor_base_url"],
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        self.assertEqual(identity["extractor_effective_provider"], "dashscope")

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
