from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evaluation.tools import run_memdaily_benchmark as memdaily_runner
from evaluation.tools import run_perltqa_benchmark as perltqa_runner
from hl_mem.evaluation.perltqa import PerLTQACharacter, PerLTQAQuestion
from hl_mem.settings import Settings
from hl_mem.storage.database import Database


def _memdaily_trajectory() -> memdaily_runner.MemDailyTrajectory:
    return memdaily_runner.MemDailyTrajectory(
        case_id="memdaily:simple:events:1",
        qtype="simple",
        subtype="events",
        tid=1,
        namespace="memdaily-simple-events-1",
        question="What happened?",
        answer="An event",
        question_at="2026-08-02T00:00:00+00:00",
        ground_truth_choice=None,
        choices={},
        messages=(
            memdaily_runner.MemDailyMessage(
                mid=1,
                event_id="memdaily:simple:events:1:mid:1",
                occurred_at="2026-08-01T00:00:00+00:00",
                text="An event happened",
                place="home",
            ),
        ),
        gold_event_ids=("memdaily:simple:events:1:mid:1",),
    )


class MemDailyAggregationTests(unittest.TestCase):
    def test_aggregate_reads_qa_metrics_from_nested_payload(self) -> None:
        results = [
            {
                "qtype": "simple",
                "error": None,
                "qa": {"exact_match": True, "f1": 0.75, "choice_correct": True},
                "retrieval": {"recall_at_5": 0.5},
            },
            {
                "qtype": "simple",
                "error": None,
                "qa": {"exact_match": False, "f1": 0.25, "choice_correct": False},
                "retrieval": {"recall_at_5": 1.0},
            },
        ]

        metrics = memdaily_runner.aggregate_results(results)

        self.assertEqual(metrics["overall"]["accuracy"], 0.5)
        self.assertEqual(metrics["overall"]["f1"], 0.5)
        self.assertEqual(metrics["overall"]["choice_accuracy"], 0.5)
        self.assertEqual(metrics["by_type"]["simple"]["accuracy"], 0.5)

    def test_cached_ingest_requires_matching_manifest_and_database_version(self) -> None:
        trajectory = _memdaily_trajectory()
        settings = Settings.for_test()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "case.db"
            manifest_path = root / "case.manifest.json"
            database = Database(db_path, settings=settings)
            connection = database.open()
            connection.execute(
                "INSERT INTO claims(id,namespace_key,recorded_from,status,extractor_version) VALUES(?,?,?,?,?)",
                (
                    "claim-1",
                    trajectory.namespace,
                    "2026-08-01T00:00:00+00:00",
                    "active",
                    memdaily_runner.LLM_EXTRACTOR_VERSION,
                ),
            )
            connection.commit()
            manifest_path.write_text(
                json.dumps(memdaily_runner._cache_identity(trajectory, settings)),
                encoding="utf-8",
            )

            reusable, reason = memdaily_runner._validate_cached_ingest(
                manifest_path,
                trajectory,
                settings,
                connection,
            )
            self.assertTrue(reusable)
            self.assertEqual(reason, "cache_valid")

            connection.execute("UPDATE claims SET extractor_version='llm-v2+stale'")
            connection.commit()
            reusable, reason = memdaily_runner._validate_cached_ingest(
                manifest_path,
                trajectory,
                settings,
                connection,
            )
            database.close()

        self.assertFalse(reusable)
        self.assertIn("database_extractor_version", reason)

    def test_cached_ingest_rejects_missing_corrupt_or_stale_manifest(self) -> None:
        trajectory = _memdaily_trajectory()
        settings = Settings.for_test()
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "case.manifest.json"
            reusable, reason = memdaily_runner._validate_cached_ingest(
                manifest_path,
                trajectory,
                settings,
            )
            self.assertFalse(reusable)
            self.assertEqual(reason, "manifest_missing")

            manifest_path.write_text("{", encoding="utf-8")
            reusable, reason = memdaily_runner._validate_cached_ingest(
                manifest_path,
                trajectory,
                settings,
            )
            self.assertFalse(reusable)
            self.assertEqual(reason, "manifest_invalid")

            stale = memdaily_runner._cache_identity(trajectory, settings)
            stale["extractor_version"] = "llm-v2+stale"
            manifest_path.write_text(json.dumps(stale), encoding="utf-8")
            reusable, reason = memdaily_runner._validate_cached_ingest(
                manifest_path,
                trajectory,
                settings,
            )

        self.assertFalse(reusable)
        self.assertIn("manifest_mismatch:extractor_version", reason)

    def test_skip_ingest_reingests_when_database_version_is_stale(self) -> None:
        trajectory = _memdaily_trajectory()
        settings = Settings.for_test()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / f"{memdaily_runner._safe_case_name(trajectory.case_id)}.db"
            database = Database(db_path, settings=settings)
            connection = database.open()
            connection.execute(
                "INSERT INTO claims(id,namespace_key,recorded_from,status,extractor_version) VALUES(?,?,?,?,?)",
                (
                    "stale-claim",
                    trajectory.namespace,
                    "2026-08-01T00:00:00+00:00",
                    "active",
                    "llm-v2+stale",
                ),
            )
            connection.commit()
            database.close()
            db_path.with_suffix(".manifest.json").write_text(
                json.dumps(memdaily_runner._cache_identity(trajectory, settings)),
                encoding="utf-8",
            )

            with (
                patch.object(memdaily_runner, "DATABASE_ROOT", root),
                patch.object(
                    memdaily_runner,
                    "_ingest_trajectory",
                    return_value={"messages": 1, "stored_claims": 0},
                ) as ingest,
                patch.object(
                    memdaily_runner,
                    "_recall_trajectory",
                    return_value=({"recall_at_5": 0.0}, []),
                ),
            ):
                result = memdaily_runner._run_case(
                    trajectory,
                    settings,
                    object(),
                    object(),
                    skip_ingest=True,
                    run_qa=False,
                    clean=False,
                    case_number=1,
                    total=1,
                )

        ingest.assert_called_once()
        self.assertIsNone(result["error"])
        self.assertEqual(result["ingest"]["cache_status"], "stale_reingested")
        self.assertIn("database_extractor_version", result["ingest"]["cache_reason"])


class PerLTQABenchmarkTests(unittest.TestCase):
    def test_reference_mapping_prefers_exact_key_then_falls_back_from_ordinal(self) -> None:
        source_keys = {
            "18_1_1": "aggregate-claim",
            "18_2_0": "wrong-aggregate-claim",
            "18_2_0#6": "dialogue-claim",
        }

        mapped = perltqa_runner._map_gold_claim_ids(("18_1_1#4", "18_2_0#6", "18_1_1#4"), source_keys)

        self.assertEqual(mapped, ["aggregate-claim", "dialogue-claim"])

    def test_character_error_counts_as_failure_without_questions(self) -> None:
        overall = perltqa_runner._aggregate_overall(
            [{"character": "broken", "error": "database failed", "questions": []}]
        )

        self.assertEqual(overall["total_errors"], 1)
        self.assertEqual(overall["successful_questions"], 0)

    def test_empty_questions_count_as_failure_without_character_error(self) -> None:
        overall = perltqa_runner._aggregate_overall([{"character": "empty", "error": None, "questions": []}])

        self.assertEqual(overall["total_errors"], 1)

    def test_remove_db_artifacts_propagates_permission_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "locked.db"
            db_path.write_bytes(b"")

            with (
                patch.object(perltqa_runner, "DATABASE_ROOT", root),
                patch.object(Path, "unlink", side_effect=PermissionError("locked")),
            ):
                with self.assertRaises(PermissionError):
                    perltqa_runner._remove_db_artifacts(db_path)

    def test_character_cleanup_retries_then_propagates_permission_error(self) -> None:
        character = PerLTQACharacter(name="locked", claims=(), questions=())

        with (
            patch.object(
                perltqa_runner,
                "_remove_db_artifacts",
                side_effect=PermissionError("locked"),
            ) as remove,
            patch.object(perltqa_runner.time, "sleep"),
        ):
            with self.assertRaises(PermissionError):
                perltqa_runner._run_character(
                    character,
                    None,  # type: ignore[arg-type]
                    None,
                    None,
                    case_number=1,
                    total=1,
                    clean=True,
                )

        self.assertEqual(remove.call_count, 4)

    def test_main_disables_query_expansion_and_fails_on_character_error(self) -> None:
        character = PerLTQACharacter(
            name="character",
            claims=(),
            questions=(
                PerLTQAQuestion(
                    question="question",
                    reference_keys=("Gender",),
                    category="profile",
                    answer="answer",
                ),
            ),
        )
        cases = (
            (
                {
                    "character": character.name,
                    "error": None,
                    "questions": [
                        {
                            "category": "profile",
                            "error": None,
                            "recall_at_5": 1.0,
                            "mrr": 1.0,
                        }
                    ],
                },
                0,
            ),
            (
                {
                    "character": character.name,
                    "error": "RuntimeError: database failed",
                    "questions": [],
                },
                1,
            ),
        )

        for character_result, expected_exit_code in cases:
            with self.subTest(expected_exit_code=expected_exit_code):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    mem_source = root / "perltmem.json"
                    qa_source = root / "perltqa.json"
                    output = root / "report.json"
                    mem_source.write_text("[]", encoding="utf-8")
                    qa_source.write_text("[]", encoding="utf-8")

                    with (
                        patch.object(
                            perltqa_runner,
                            "load_settings",
                            return_value=Settings(),
                        ),
                        patch.object(perltqa_runner, "initialize_process"),
                        patch.object(
                            perltqa_runner,
                            "make_embedder",
                            return_value=object(),
                        ),
                        patch.object(
                            perltqa_runner,
                            "make_reranker",
                            return_value=object(),
                        ),
                        patch.object(
                            perltqa_runner.PerLTQAAdapter,
                            "load",
                            return_value=[character],
                        ),
                        patch.object(
                            perltqa_runner,
                            "_run_character",
                            return_value=character_result,
                        ) as run_character,
                        patch.object(perltqa_runner, "_write_json_atomic"),
                    ):
                        exit_code = perltqa_runner.main(
                            [
                                "--source",
                                str(mem_source),
                                "--qa-source",
                                str(qa_source),
                                "--output",
                                str(output),
                            ]
                        )

                self.assertEqual(exit_code, expected_exit_code)
                benchmark_settings = run_character.call_args.args[1]
                self.assertEqual(benchmark_settings.query_expansion_mode, "off")


if __name__ == "__main__":
    unittest.main()
