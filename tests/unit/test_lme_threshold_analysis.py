from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hl_mem.core.vector import pack_vector
from hl_mem.ingest.chunking import ChunkingPolicy
from hl_mem.ingest.llm_extractor import LLMExtractor
from hl_mem.llm.types import LLMRequest, LLMResponse
from scripts.run_longmemeval_benchmark import (
    DATABASE_ROOT,
    EMBEDDING_CONFIGS,
    _backup_claims_file,
    _claim_similarity_records,
    _remove_compare_variants,
    _similarity_distribution,
    normalize_case,
)


class _RecordingLLMClient:
    class _Provider:
        name = "fake"

    provider = _Provider()
    model = "fake-model"

    def __init__(self) -> None:
        self.last_request: LLMRequest | None = None

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.last_request = request
        return LLMResponse('{"claims":[],"should_memorize":false}', "stop", 1)


class PromptRollbackTests(unittest.TestCase):
    def test_extraction_request_uses_only_original_language_rule(self) -> None:
        client = _RecordingLLMClient()
        extractor = LLMExtractor(client, ChunkingPolicy(10_000, 0, 2))

        extractor.extract("I prefer concise answers.")

        self.assertIsNotNone(client.last_request)
        system = client.last_request.messages[0].content
        user = client.last_request.messages[1].content
        self.assertIn("保留用户原始语言：中文原文输出中文，英文原文输出英文。", system)
        self.assertNotIn("Output claims in the language of the input conversation", system)
        self.assertNotIn("LANGUAGE RULE", system)
        self.assertNotIn("[Language: Match the language", user)
        self.assertNotIn("IMPORTANT: Output all claims in the same language", user)


class ThresholdAnalysisTests(unittest.TestCase):
    def test_claim_similarity_records_score_both_targets_and_mark_answer_session(self) -> None:
        case = normalize_case(
            {
                "question_id": "case-1",
                "question_type": "multi-session",
                "question": "What degree did I graduate with?",
                "answer": "Business Administration",
                "answer_session_ids": ["answer-session"],
                "haystack_dates": ["2024-01-01", "2024-01-02"],
                "haystack_session_ids": ["distractor-session", "answer-session"],
                "haystack_sessions": [
                    [{"role": "user", "content": "I like jazz."}],
                    [{"role": "user", "content": "I studied Business Administration."}],
                ],
            }
        )
        claims = [
            {
                "claim_id": "degree",
                "value": "Business Administration",
                "evidence_event_ids": [case.gold_event_ids[0]],
            },
            {
                "claim_id": "music",
                "value": "jazz",
                "evidence_event_ids": [case.sessions[0].event_id],
            },
        ]

        class FakeEmbedder:
            vectors = {
                "Business Administration": pack_vector([1.0, 0.0]),
                "jazz": pack_vector([0.0, 1.0]),
            }

            def embed_batch(self, texts: list[str]) -> list[bytes]:
                return [self.vectors[text] for text in texts]

            def embed_query(self, text: str) -> bytes:
                self.seen_query = text
                return pack_vector([0.8, 0.6])

        records = _claim_similarity_records(claims, case, FakeEmbedder())

        self.assertEqual([record["claim_id"] for record in records], ["degree", "music"])
        self.assertAlmostEqual(records[0]["answer_similarity"], 1.0)
        self.assertAlmostEqual(records[0]["question_similarity"], 0.8)
        self.assertEqual(records[0]["evidence_session_ids"], ["answer-session"])
        self.assertTrue(records[0]["from_answer_session"])
        self.assertEqual(records[0]["evidence_session_ids"], ["answer-session"])
        self.assertAlmostEqual(records[1]["answer_similarity"], 0.0)
        self.assertAlmostEqual(records[1]["question_similarity"], 0.6)
        self.assertFalse(records[1]["from_answer_session"])

    def test_similarity_distribution_uses_linear_percentiles_and_strict_thresholds(self) -> None:
        distribution = _similarity_distribution([0.1, 0.3, 0.5, 0.7])

        self.assertEqual(distribution["count"], 4)
        self.assertAlmostEqual(distribution["max"], 0.7)
        self.assertAlmostEqual(distribution["median"], 0.4)
        self.assertAlmostEqual(distribution["p75"], 0.55)
        self.assertAlmostEqual(distribution["p90"], 0.64)
        self.assertAlmostEqual(distribution["p95"], 0.67)
        self.assertEqual(
            distribution["counts_above_threshold"],
            {">0.2": 3, ">0.3": 2, ">0.4": 2, ">0.5": 1, ">0.65": 1},
        )

    def test_similarity_distribution_handles_no_claims(self) -> None:
        distribution = _similarity_distribution([])

        self.assertEqual(distribution["count"], 0)
        self.assertIsNone(distribution["max"])
        self.assertIsNone(distribution["median"])
        self.assertEqual(
            distribution["counts_above_threshold"], {">0.2": 0, ">0.3": 0, ">0.4": 0, ">0.5": 0, ">0.65": 0}
        )


class ArtifactPreservationTests(unittest.TestCase):
    def test_backup_copies_claims_without_touching_other_cache_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "live" / "claims.json"
            source.parent.mkdir()
            source.write_text(json.dumps({"case_id": "case/1", "claims": []}), encoding="utf-8")
            backup_root = root / "backup"
            unrelated = backup_root / "keep.txt"
            unrelated.parent.mkdir()
            unrelated.write_text("keep", encoding="utf-8")

            backup = _backup_claims_file("case/1", source, backup_root)

            self.assertEqual(backup.read_bytes(), source.read_bytes())
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")

    def test_compare_cleanup_removes_only_variant_databases(self) -> None:
        DATABASE_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=DATABASE_ROOT) as directory:
            case_dir = Path(directory)
            base = case_dir / "base.db"
            claims = case_dir / "claims.json"
            manifest = case_dir / "base.manifest.json"
            for path in (base, claims, manifest):
                path.write_text(path.name, encoding="utf-8")
            for code in EMBEDDING_CONFIGS:
                (case_dir / f"{code}.db").write_text(code, encoding="utf-8")

            _remove_compare_variants(case_dir)

            self.assertTrue(base.is_file())
            self.assertTrue(claims.is_file())
            self.assertTrue(manifest.is_file())
            self.assertTrue(all(not (case_dir / f"{code}.db").exists() for code in EMBEDDING_CONFIGS))


if __name__ == "__main__":
    unittest.main()
