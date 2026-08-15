from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

import tests.eval.chinese_e2e as chinese_e2e
from tests.eval.chinese_e2e import (
    DEFAULT_THRESHOLDS,
    DatasetThresholds,
    SourceHashMismatch,
    aggregate_results,
    build_e2e_report,
    build_perltqa_ingest_trajectory,
    build_perltqa_question_trajectory,
    covered_gold_events,
    evaluate_gate,
    load_sample_manifest,
    load_sampled_inputs,
    normalize_benchmark_case,
    remaining_bundle_questions,
    score_answer_anchors,
    score_retrieved_evidence,
    verify_source_hashes,
)

SAMPLE_MANIFEST_PATH = Path(__file__).parent / "fixtures" / "chinese_e2e_sample.json"
RUBRIC_V2_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "chinese_e2e_rubric_v2.json"


def test_manifest_keeps_the_paid_sample_fixed_and_stratified() -> None:
    """Removing a slice or silently changing the paid sample must break offline validation."""
    manifest = load_sample_manifest(SAMPLE_MANIFEST_PATH)

    assert manifest.schema_version == 3
    assert manifest.perltqa_question_count == 28
    assert manifest.memdaily_question_count == 12
    assert {dataset: len(case_ids) for dataset, case_ids in manifest.expected_case_ids.items()} == {
        "perltqa": 28,
        "memdaily": 12,
    }
    assert len(set(manifest.expected_case_ids["perltqa"])) == 28
    assert "perltqa:0709ec234e33:dialogues:77cdfec7b17e" in manifest.expected_case_ids["perltqa"]
    assert "perltqa:0709ec234e33:dialogues:5b862015f4c8" not in manifest.expected_case_ids["perltqa"]
    assert manifest.slice_counts == {
        "memdaily_aggregative": 2,
        "memdaily_comparative": 2,
        "memdaily_conditional": 2,
        "memdaily_noisy": 2,
        "memdaily_post_processing": 2,
        "memdaily_simple": 2,
        "perltqa_dialogues": 8,
        "perltqa_events": 8,
        "perltqa_profile": 4,
        "perltqa_social_relationship": 8,
    }


def test_source_hash_check_rejects_changed_upstream_before_paid_calls(tmp_path: Path) -> None:
    """A changed private dataset must not silently evaluate a different sample."""
    source = tmp_path / "source.json"
    source.write_text("{}", encoding="utf-8")
    expected = hashlib.sha256(source.read_bytes()).hexdigest()

    verify_source_hashes({"fixture": {"path": str(source), "sha256": expected}})
    source.write_text('{"changed": true}', encoding="utf-8")

    with pytest.raises(SourceHashMismatch, match="fixture"):
        verify_source_hashes({"fixture": {"path": str(source), "sha256": expected}})


def test_committed_sample_resolves_to_exact_questions_when_private_data_is_installed() -> None:
    """Stale persona/source/question IDs must fail before the network is touched."""
    manifest = load_sample_manifest(SAMPLE_MANIFEST_PATH)
    if not all(Path(item["path"]).is_file() for item in manifest.sources.values()):
        pytest.skip("private Chinese E2E sources are not installed")

    sampled = load_sampled_inputs(manifest)

    assert len(sampled.perltqa_bundles) == 4
    assert sum(len(bundle.questions) for bundle in sampled.perltqa_bundles) == 28
    assert len(sampled.memdaily_trajectories) == 12
    assert {trajectory.qtype for trajectory in sampled.memdaily_trajectories} == {
        "simple",
        "conditional",
        "comparative",
        "aggregative",
        "post_processing",
        "noisy",
    }
    corrected = next(
        question
        for bundle in sampled.perltqa_bundles
        for question in bundle.questions
        if question.case_id == "perltqa:0709ec234e33:dialogues:77cdfec7b17e"
    )
    assert corrected.question == "陈刚对什么的拍摄有见解？"
    assert corrected.answer == "陈刚对电影的拍摄有很多见解。"
    assert corrected.answer_anchors == ("电影",)


def test_aggregate_scores_questions_but_counts_shared_extraction_units_once() -> None:
    """PerLTQA questions sharing one source must not inflate extraction coverage."""
    report = aggregate_results(
        [
            {
                "dataset": "perltqa",
                "case_id": "q1",
                "slice": "perltqa_events",
                "error": None,
                "qa": {"exact_match": 1.0, "f1": 1.0},
                "retrieval": {"recall_at_5": 1.0, "mrr": 1.0},
                "gold_extraction_units": ["event-1"],
                "covered_extraction_units": ["event-1"],
            },
            {
                "dataset": "perltqa",
                "case_id": "q2",
                "slice": "perltqa_events",
                "error": None,
                "qa": {"exact_match": 0.0, "f1": 0.5},
                "retrieval": {"recall_at_5": 1.0, "mrr": 0.5},
                "gold_extraction_units": ["event-1"],
                "covered_extraction_units": ["event-1"],
            },
            {
                "dataset": "perltqa",
                "case_id": "q3",
                "slice": "perltqa_profile",
                "error": None,
                "qa": {"exact_match": 1.0, "f1": 0.9},
                "retrieval": {"recall_at_5": 0.0, "mrr": 0.0},
                "gold_extraction_units": ["event-2"],
                "covered_extraction_units": [],
            },
        ]
    )

    overall = report["overall"]
    assert overall == {
        "cases": 3,
        "successful_cases": 3,
        "failed_cases": 0,
        "qa_accuracy": pytest.approx(2 / 3),
        "qa_f1": pytest.approx(0.8),
        "recall_at_5": pytest.approx(2 / 3),
        "mrr": pytest.approx(0.5),
        "extraction_coverage": pytest.approx(0.5),
        "gold_extraction_units": 2,
        "covered_extraction_units": 1,
    }
    assert report["by_slice"]["perltqa_events"]["cases"] == 2


def test_gate_accepts_exact_boundaries_and_rejects_errors_or_regressions() -> None:
    """Rounding and stochastic slack must not turn a boundary pass into a failure."""
    thresholds = {
        "perltqa": DatasetThresholds(
            qa_accuracy=0.60,
            qa_f1=0.68,
            recall_at_5=0.68,
            mrr=0.50,
            extraction_coverage=0.75,
        )
    }
    passing = {
        "perltqa": {
            "failed_cases": 0,
            "qa_accuracy": 0.60,
            "qa_f1": 0.68,
            "recall_at_5": 0.68,
            "mrr": 0.50,
            "extraction_coverage": 0.75,
        }
    }
    assert evaluate_gate(passing, thresholds) == ()

    failing = {"perltqa": {**passing["perltqa"], "failed_cases": 1, "recall_at_5": 0.679}}
    failures = evaluate_gate(failing, thresholds)

    assert [(failure.metric, failure.actual, failure.minimum) for failure in failures] == [
        ("failed_cases", 1.0, 0.0),
        ("recall_at_5", 0.679, 0.68),
    ]
    assert DEFAULT_THRESHOLDS["memdaily"].qa_accuracy == 0.75
    assert DEFAULT_THRESHOLDS["perltqa"].qa_accuracy == 0.85


def test_overall_qa_gate_enforces_the_ninety_percent_target() -> None:
    assert chinese_e2e.evaluate_overall_gate({"qa_accuracy": 0.90}) == ()

    failures = chinese_e2e.evaluate_overall_gate({"qa_accuracy": 0.899})

    assert [(failure.dataset, failure.metric, failure.actual, failure.minimum) for failure in failures] == [
        ("overall", "qa_accuracy", 0.899, 0.90)
    ]


def test_perltqa_bundle_becomes_one_shared_ingest_and_question_specific_views() -> None:
    """Splitting a persona into per-question extraction would multiply cost and change evidence."""
    manifest = load_sample_manifest(SAMPLE_MANIFEST_PATH)
    if not all(Path(item["path"]).is_file() for item in manifest.sources.values()):
        pytest.skip("private Chinese E2E sources are not installed")
    bundle = load_sampled_inputs(manifest).perltqa_bundles[0]

    ingest = build_perltqa_ingest_trajectory(bundle)
    question_view = build_perltqa_question_trajectory(ingest, bundle.questions[0])

    assert ingest.case_id.startswith("perltqa:e2e:ingest:")
    assert ingest.namespace == bundle.namespace
    assert len(ingest.messages) == 8
    assert ingest.gold_event_ids == ()
    assert question_view.messages == ingest.messages
    assert question_view.question == bundle.questions[0].question
    assert question_view.answer == bundle.questions[0].answer
    assert question_view.gold_event_ids == bundle.questions[0].gold_event_ids
    assert question_view.question_at == "2026-08-14T00:00:00+00:00"
    assert replace(question_view, question="changed").messages == ingest.messages


def test_retrieved_evidence_scores_first_gold_rank_and_hit_at_five() -> None:
    """A gold source below rank five must not be counted as Recall@5."""
    retrieved = [
        {"rank": 1, "evidence_event_ids": ["noise-1"]},
        {"rank": 2, "evidence_event_ids": ["gold"]},
        {"rank": 6, "evidence_event_ids": ["other-gold"]},
    ]

    assert score_retrieved_evidence(retrieved, ("gold",), k=5) == {"recall_at_5": 1.0, "mrr": 0.5}
    assert score_retrieved_evidence(retrieved, ("other-gold",), k=5) == {"recall_at_5": 0.0, "mrr": 1 / 6}


def test_extraction_coverage_reads_all_stored_evidence_not_only_retrieved_claims() -> None:
    """A correctly extracted but low-ranked claim still counts as extraction coverage."""
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE evidence_links (derived_type TEXT, derived_id TEXT, evidence_type TEXT, evidence_id TEXT)"
    )
    connection.executemany(
        "INSERT INTO evidence_links VALUES ('claim', ?, 'event', ?)",
        [("claim-1", "gold-1"), ("claim-2", "distractor")],
    )

    assert covered_gold_events(connection, ("gold-1", "gold-2")) == ("gold-1",)


def test_benchmark_case_normalization_adds_mrr_and_unified_stage_fields() -> None:
    """Dropping provenance while adapting an existing runner would hide extraction loss."""
    normalized = normalize_benchmark_case(
        dataset="memdaily",
        slice_name="memdaily_simple",
        ingest_unit_id="memdaily:simple:events:1",
        raw={
            "case_id": "memdaily:simple:events:1",
            "question": "问题",
            "answer": "答案",
            "gold_event_ids": ["gold"],
            "ingest": {"cache_status": "reused", "skipped": True},
            "retrieval": {"recall_at_5": 1.0},
            "retrieved": [
                {"rank": 1, "evidence_event_ids": ["noise"]},
                {"rank": 2, "evidence_event_ids": ["gold"]},
            ],
            "qa": {"exact_match": 1.0, "f1": 0.9},
            "error": None,
        },
        covered_event_ids=("gold",),
    )

    assert normalized["dataset"] == "memdaily"
    assert normalized["slice"] == "memdaily_simple"
    assert normalized["ingest_unit_id"] == "memdaily:simple:events:1"
    assert normalized["retrieval"] == {"recall_at_5": 1.0, "mrr": 0.5}
    assert normalized["gold_extraction_units"] == ["gold"]
    assert normalized["covered_extraction_units"] == ["gold"]
    assert normalized["qa"] == {"exact_match": 1.0, "f1": 0.9}


def test_report_exposes_scores_gate_failures_cache_and_token_costs() -> None:
    """A pytest pass/fail without auditable scores and paid-call accounting is not an evaluation report."""
    manifest = load_sample_manifest(SAMPLE_MANIFEST_PATH)
    case = {
        "dataset": "memdaily",
        "case_id": "one",
        "slice": "memdaily_simple",
        "ingest_unit_id": "ingest-one",
        "error": None,
        "ingest": {
            "cache_status": "fresh_ingest",
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
        },
        "qa": {"exact_match": 1.0, "f1": 1.0, "usage": {"total_tokens": 30}},
        "retrieval": {"recall_at_5": 1.0, "mrr": 1.0},
        "gold_extraction_units": ["gold"],
        "covered_extraction_units": ["gold"],
    }

    report = build_e2e_report(
        manifest=manifest,
        cases=[case],
        models={"extractor": "extract", "embedder": "embed", "reranker": "rerank", "qa": "qa"},
        status="completed",
        refresh=True,
    )

    assert report["benchmark"] == "chinese_e2e"
    assert report["scorer_version"] == chinese_e2e.SCORER_VERSION
    assert report["metrics"]["overall"]["qa_accuracy"] == 1.0
    assert report["run"]["cache_status_counts"] == {"fresh_ingest": 1}
    assert report["run"]["usage"] == {
        "extraction_input_tokens": 100,
        "extraction_output_tokens": 20,
        "extraction_total_tokens": 120,
        "qa_total_tokens": 30,
    }
    assert report["gate"]["passed"] is False
    assert report["gate"]["overall_thresholds"] == {"qa_accuracy": 0.90}
    assert {failure["dataset"] for failure in report["gate"]["failures"]} == {"memdaily", "perltqa"}
    assert report["gate"]["case_contract"]["memdaily"] == {
        "expected_cases": 12,
        "actual_rows": 1,
        "missing_case_ids": list(manifest.expected_case_ids["memdaily"]),
        "unexpected_case_ids": ["one"],
        "duplicate_case_ids": [],
        "exact": False,
    }


def test_gate_rejects_an_incomplete_high_scoring_subset_and_collapsed_slice() -> None:
    """A cherry-picked subset must not pass, nor may a complete slice lose extraction/recall."""
    manifest = load_sample_manifest(SAMPLE_MANIFEST_PATH)
    cases = []
    expected_slices = {
        case_id: slice_name
        for slice_name, case_ids in manifest.expected_case_ids_by_slice.items()
        for case_id in case_ids
    }
    for dataset, case_ids in manifest.expected_case_ids.items():
        for case_id in case_ids:
            slice_name = expected_slices[case_id]
            cases.append(
                {
                    "dataset": dataset,
                    "case_id": case_id,
                    "slice": slice_name,
                    "error": None,
                    "qa": {"exact_match": 1.0, "f1": 1.0},
                    "retrieval": {"recall_at_5": 1.0, "mrr": 1.0},
                    "gold_extraction_units": [case_id],
                    "covered_extraction_units": [case_id],
                }
            )

    incomplete = build_e2e_report(
        manifest=manifest,
        cases=cases[:-1],
        models={},
        status="completed",
        refresh=False,
    )
    assert incomplete["gate"]["passed"] is False
    assert incomplete["gate"]["case_contract"]["memdaily"]["missing_case_ids"] == [cases[-1]["case_id"]]

    events_slice = "perltqa_events"
    dialogues_slice = "perltqa_dialogues"
    mislabeled = [dict(case) for case in cases]
    for case in mislabeled:
        if case["slice"] == events_slice:
            case["slice"] = dialogues_slice
        elif case["slice"] == dialogues_slice:
            case["slice"] = events_slice
    swapped = build_e2e_report(
        manifest=manifest,
        cases=mislabeled,
        models={},
        status="completed",
        refresh=False,
    )
    assert swapped["gate"]["passed"] is False
    assert swapped["gate"]["slice_contract"][events_slice]["exact"] is False
    assert swapped["gate"]["slice_contract"][dialogues_slice]["exact"] is False

    collapsed_slice = manifest.expected_case_ids_by_slice["perltqa_events"]
    for case in cases:
        if case["case_id"] in collapsed_slice:
            case["retrieval"]["recall_at_5"] = 0.0
            case["covered_extraction_units"] = []
    collapsed = build_e2e_report(
        manifest=manifest,
        cases=cases,
        models={},
        status="completed",
        refresh=False,
    )
    assert collapsed["gate"]["passed"] is False
    assert {failure["metric"] for failure in collapsed["gate"]["failures"] if failure["dataset"] == "perltqa"} >= {
        "perltqa_events.recall_at_5",
        "perltqa_events.extraction_coverage",
    }


def test_perltqa_qa_accuracy_uses_all_official_memory_anchors() -> None:
    """A concise correct value must pass while an incomplete multi-anchor answer fails."""
    assert score_answer_anchors("佳佳", ("佳佳",)) == 1.0
    assert score_answer_anchors("熊飞今年30岁", ("30",)) == 1.0
    assert score_answer_anchors("原油和黄金", ("原油", "黄金", "铜")) == 0.0
    assert score_answer_anchors("信息不足", ("刘晓",)) == 0.0


def test_reviewed_rubrics_use_or_between_rubrics_and_and_between_concepts() -> None:
    accepted_rubrics = (
        (("自由有界限", "自由不是无限"), ("独立思考", "独立想法")),
        (("同学", "高中同学"),),
    )

    assert chinese_e2e.score_answer("官方答案", ("官方答案",), accepted_rubrics) == {
        "answer_correct": 1.0,
        "verdict_basis": "official_anchor",
        "scorer_version": chinese_e2e.SCORER_VERSION,
    }
    assert chinese_e2e.score_answer("高中同学", ("熟人",), accepted_rubrics) == {
        "answer_correct": 1.0,
        "verdict_basis": "reviewed_rubric",
        "scorer_version": chinese_e2e.SCORER_VERSION,
    }
    assert (
        chinese_e2e.score_answer("自由不是无限，也要保持独立思考", ("逐字锚点",), accepted_rubrics)["answer_correct"]
        == 1.0
    )
    assert chinese_e2e.score_answer("自由有界限", ("逐字锚点",), accepted_rubrics) == {
        "answer_correct": 0.0,
        "verdict_basis": "reviewed_rubric",
        "scorer_version": chinese_e2e.SCORER_VERSION,
    }


def test_cases_without_reviewed_rubrics_keep_legacy_anchor_scoring() -> None:
    assert chinese_e2e.score_answer("同义改写", ("官方锚点",), ()) == {
        "answer_correct": 0.0,
        "verdict_basis": "official_anchor",
        "scorer_version": chinese_e2e.SCORER_VERSION,
    }


def test_manifest_rubrics_accept_the_reviewed_semantic_answers() -> None:
    manifest = load_sample_manifest(SAMPLE_MANIFEST_PATH)
    reviewed_cases = {
        "7b6b2c31b857d5dd39443ebb8c71f3585eedae14881382e51c0bd9de839fce23": (
            "高中同学",
            ("熟人",),
        ),
        "5a9b1bb8a2bb8eda162a6ed195d4b3c3bf2bfc29401231e60b9cb53947c25aa4": (
            "同学",
            ("熟人",),
        ),
        "7336d023b16ece6a96cdf8742a314e9aea99b582bdcc7334868172ddd93b8a41": (
            "因为对电影故事情节着迷。",
            ("故事情节", "着迷", "学了这个专业"),
        ),
        "836f6182a0a955c32b52e70749b63e25c2c81d418821e6537435be6d9670a601": (
            "自由有界限，需与他人共处；实践中应保留独立想法，发现真正欲望。",
            ("每个人", "自由", "界限", "独立", "想法", "选择", "发现", "真正欲望"),
        ),
        "375593a841228979606b44031bbe8300f52d8d1ef504b4ad5f1d0d1f0a29ecd5": (
            "癌症细胞存在染色体重排或结构异常，可能影响基因的表达和调控。",
            ("染色体三维结构", "癌症发展", "关系", "研究人员", "异常", "影响"),
        ),
        "0d532ef7647f7a2002432d3c394bfdd8648b200d884244d0487bc3ab0a5344d0": (
            "给人以触动。",
            ("新生婴儿", "真实状态", "触动"),
        ),
        "362879793b73394167576d78afadfcc54cd37eaa8fe1f33fec3b606a738aba84": (
            "张强是小飞（熊飞）的同学，中国建筑师。",
            ("小飞", "31岁", "中国", "中等身材", "短发", "建筑师", "篮球", "摄影"),
        ),
        "24f99bbbc8ecba95fcab2d1ea95dd3843f13202d41adeadae416d36d11bddca1": (
            "探索染色体三维结构与癌症发展之间的关系。",
            ("高分辨率染色体构象捕获技术", "肿瘤细胞样本", "荧光原位杂交技术", "染色体", "位置", "结构"),
        ),
        "4a3607094e6c3b592b9f8e07dae91cef7b4088928d116d56947c7e128ee31103": (
            "电影制作技巧和艺术表达能力。",
            ("电影相关问题",),
        ),
    }

    assert set(manifest.accepted_rubrics_by_question_hash) == set(reviewed_cases)
    for question_hash, (predicted, official_anchors) in reviewed_cases.items():
        assert score_answer_anchors(predicted, official_anchors) == 0.0
        assert chinese_e2e.score_answer(
            predicted,
            official_anchors,
            manifest.accepted_rubrics_by_question_hash[question_hash],
        ) == {
            "answer_correct": 1.0,
            "verdict_basis": "reviewed_rubric",
            "scorer_version": chinese_e2e.SCORER_VERSION,
        }


def test_rubric_v2_fixture_rejects_incomplete_and_modality_promoted_answers() -> None:
    fixture = json.loads(RUBRIC_V2_FIXTURE_PATH.read_text(encoding="utf-8"))

    assert fixture["schema_version"] == 2
    assert fixture["scorer_version"] == chinese_e2e.SCORER_VERSION
    assert fixture["data_classification"] == "synthetic_public"
    assert {case["category"] for case in fixture["cases"]} == {
        "enumeration_completeness",
        "concise_semantic_answer",
        "recommendation_not_execution",
    }
    for case in fixture["cases"]:
        accepted_rubrics = tuple(
            tuple(tuple(str(alias) for alias in concept) for concept in rubric["required_concepts"])
            for rubric in case["accepted_rubrics"]
        )
        result = chinese_e2e.score_answer(
            case["predicted_answer"],
            tuple(case["official_anchors"]),
            accepted_rubrics,
        )
        assert bool(result["answer_correct"]) is case["expected_correct"], case["case_id"]


def test_bundle_failure_recovery_does_not_duplicate_completed_questions() -> None:
    """A late bundle error must retain exactly one row per paid question."""
    manifest = load_sample_manifest(SAMPLE_MANIFEST_PATH)
    if not all(Path(item["path"]).is_file() for item in manifest.sources.values()):
        pytest.skip("private Chinese E2E sources are not installed")
    bundle = load_sampled_inputs(manifest).perltqa_bundles[0]

    remaining = remaining_bundle_questions(bundle, [{"case_id": bundle.questions[0].case_id}])

    assert [question.case_id for question in remaining] == [question.case_id for question in bundle.questions[1:]]
