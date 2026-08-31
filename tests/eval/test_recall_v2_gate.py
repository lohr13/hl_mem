"""recall_v2 runner 与发布门禁测试。"""

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from hl_mem.core.vector import pack_vector
from hl_mem.settings import Settings
from tests.eval import ci_gate, eval_runner
from tests.eval.gate_check import check


def test_public_recall_gate_runs_without_private_artifacts(tmp_path: Path) -> None:
    report = tmp_path / "public-recall-report.json"

    assert ci_gate.main(["--report", str(report)]) == 0
    assert report.is_file()


def test_score_consumes_answerability_and_reports_min_relevance() -> None:
    """answerability 驱动无答案诊断，min_relevance 仅进入诊断输出。"""
    row = {
        "id": "case-1",
        "slice": "no_answer",
        "expected_claim_ids": [],
        "equivalent_ids": [],
        "forbidden_ids": [],
        "min_relevance": "none",
    }

    score = eval_runner._score(
        row,
        {"results": [], "answerability": "no_evidence"},
        latency_ms=1.0,
        top_k=5,
    )

    assert score["predicted_no_answer"] is True
    assert score["abstention_kind"] == "hard"
    assert score["low_confidence"] is False
    assert score["min_relevance_diagnostic"] == "not yet used for scoring"


def test_metrics_count_both_abstentions_and_report_hard_soft_separately() -> None:
    """把 soft 漏出总体拒答或并入 hard 指标时必须失败。"""
    items = [
        {
            "answerable": False,
            "predicted_no_answer": True,
            "hard_abstention": True,
            "soft_abstention": False,
            "low_confidence": False,
        },
        {
            "answerable": False,
            "predicted_no_answer": True,
            "hard_abstention": False,
            "soft_abstention": True,
            "low_confidence": True,
        },
        {
            "answerable": True,
            "predicted_no_answer": True,
            "hard_abstention": False,
            "soft_abstention": True,
            "low_confidence": True,
        },
    ]

    metrics = eval_runner._metrics(items)

    assert metrics["no_answer_precision"] == pytest.approx(2 / 3)
    assert metrics["no_answer_recall"] == 1.0
    assert metrics["no_answer_f1"] == pytest.approx(0.8)
    assert metrics["hard_abstention_precision"] == 1.0
    assert metrics["hard_abstention_recall"] == 0.5
    assert metrics["soft_abstention_precision"] == 0.5
    assert metrics["soft_abstention_recall"] == 0.5


def test_score_preserves_pair_id_and_result_raw_scores() -> None:
    """评测结果不能丢失配对关系或返回 claim 的原始评分。"""
    row = {
        "id": "gpu-colloquial",
        "pair_id": "gpu",
        "slice": "colloquial",
        "expected_claim_ids": ["claim-1"],
        "equivalent_ids": [],
        "forbidden_ids": [],
        "min_relevance": "high",
    }
    response = {
        "results": [
            {"id": "claim-1", "reranker_raw_score": 0.41},
            {"id": "claim-2", "reranker_raw_score": 0.22},
        ],
        "answerability": "supported",
        "search_trace": {
            "candidates": {
                "claim-1": {"rerank_score": 0.42},
                "claim-2": {"rerank_score": 0.21},
            }
        },
    }

    score = eval_runner._score(
        row,
        response,
        latency_ms=1.0,
        top_k=5,
        dense_raw_scores={"claim-1": 0.73, "claim-2": 0.31},
    )

    assert score["pair_id"] == "gpu"
    assert score["raw_scores"] == [
        {
            "claim_id": "claim-1",
            "rank": 1,
            "dense_raw_score": 0.73,
            "reranker_raw_score": 0.42,
        },
        {
            "claim_id": "claim-2",
            "rank": 2,
            "dense_raw_score": 0.31,
            "reranker_raw_score": 0.21,
        },
    ]


def test_distribution_reports_requested_nearest_rank_statistics() -> None:
    """raw score 摘要提供固定分位点，并把空样本与真实零分区分。"""
    assert eval_runner._distribution([]) == {
        "count": 0,
        "min": None,
        "p10": None,
        "p25": None,
        "p50": None,
        "p75": None,
        "p90": None,
        "max": None,
        "mean": None,
    }

    summary = eval_runner._distribution([float(value) for value in range(1, 11)])

    assert summary == {
        "count": 10,
        "min": 1.0,
        "p10": 1.0,
        "p25": 3.0,
        "p50": 5.0,
        "p75": 8.0,
        "p90": 9.0,
        "max": 10.0,
        "mean": 5.5,
    }


def test_dense_raw_scores_use_frozen_claim_embeddings() -> None:
    """dense raw 必须是原始 query 与冻结 claim 向量的余弦值。"""
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE claims(id TEXT PRIMARY KEY, embedding_dense BLOB)")
    connection.executemany(
        "INSERT INTO claims(id,embedding_dense) VALUES (?,?)",
        (
            ("same", pack_vector([1.0, 0.0])),
            ("orthogonal", pack_vector([0.0, 1.0])),
            ("missing-vector", None),
        ),
    )
    try:
        scores = eval_runner._dense_raw_scores(
            connection,
            pack_vector([1.0, 0.0]),
            ["same", "orthogonal", "missing-vector", "unknown"],
        )
    finally:
        connection.close()

    assert scores == {"same": pytest.approx(1.0), "orthogonal": pytest.approx(0.0)}


def test_score_distributions_group_top_results_by_answerability() -> None:
    """阈值分布按查询真值分组，且每条查询只取最终 top result。"""
    items = [
        {
            "answerable": True,
            "raw_scores": [
                {"dense_raw_score": 0.8, "reranker_raw_score": 0.6},
                {"dense_raw_score": 0.1, "reranker_raw_score": 0.05},
            ],
        },
        {
            "answerable": False,
            "raw_scores": [{"dense_raw_score": 0.2, "reranker_raw_score": None}],
        },
    ]

    distributions = eval_runner._score_distributions(items)

    assert distributions["answerable"]["dense_raw_score"]["count"] == 1
    assert distributions["answerable"]["dense_raw_score"]["mean"] == 0.8
    assert distributions["answerable"]["reranker_raw_score"]["mean"] == 0.6
    assert distributions["no_answer"]["dense_raw_score"]["mean"] == 0.2
    assert distributions["no_answer"]["reranker_raw_score"]["count"] == 0


def test_main_loads_config_and_overrides_expansion_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI 指定的 TOML、dotenv 与 expansion mode 必须到达评测运行时。"""
    captured: dict[str, object] = {}
    config_path = tmp_path / "eval.toml"
    env_path = tmp_path / ".env.eval"
    report_path = tmp_path / "report.json"

    def fake_load_settings(config: Path | None, env: Path | None) -> Settings:
        captured["config"] = config
        captured["env"] = env
        return replace(Settings.for_test(), llm_api_key="test-key")

    def fake_run(
        snapshot: Path,
        dataset: Path,
        top_k: int,
        settings: Settings | None = None,
        *,
        reference_time: str | None = None,
    ) -> dict[str, object]:
        captured["snapshot"] = snapshot
        captured["dataset"] = dataset
        captured["top_k"] = top_k
        captured["settings"] = settings
        captured["reference_time"] = reference_time
        return {"schema_version": 2}

    monkeypatch.setattr(eval_runner, "load_settings", fake_load_settings, raising=False)
    monkeypatch.setattr(eval_runner, "run", fake_run)
    monkeypatch.setattr(eval_runner, "_print_summary", lambda *_args: None)

    exit_code = eval_runner.main(
        [
            "--snapshot",
            str(tmp_path / "snapshot.db"),
            "--dataset",
            str(tmp_path / "dataset.jsonl"),
            "--config",
            str(config_path),
            "--env-file",
            str(env_path),
            "--expansion-mode",
            "auto",
            "--reference-time",
            "2026-08-02T00:00:00+00:00",
            "--report",
            str(report_path),
        ]
    )

    assert exit_code == 0
    assert captured["config"] == config_path
    assert captured["env"] == env_path
    assert captured["reference_time"] == "2026-08-02T00:00:00+00:00"
    assert isinstance(captured["settings"], Settings)
    assert captured["settings"].query_expansion_mode == "auto"
    assert report_path.is_file()


def test_gate_checks_integrity_and_safety_metrics() -> None:
    """门禁拒绝样例分布变化、禁用命中及 HTTP 失败。"""
    metrics = {
        "mrr": 1.0,
        "recall_at_5": 1.0,
        "no_answer_precision": 1.0,
        "no_answer_recall": 1.0,
    }
    baseline = {
        "status": "ready",
        "dataset_sha256": "dataset",
        "snapshot_sha256": "snapshot",
        "case_count": 1,
        "slice_counts": {"no_answer": 1},
        "metrics": metrics,
        "slices": {},
    }
    report = {
        "artifacts": {"dataset_sha256": "dataset", "snapshot_sha256": "snapshot"},
        "case_count": 2,
        "slice_counts": {"no_answer": 2},
        "metrics": metrics,
        "slices": {},
        "total_forbidden_hits": 1,
        "http_success_rate": 0.5,
    }

    failures = check(report, baseline, tolerance=0.01, slice_tolerance=0.05)

    assert any("case_count" in failure for failure in failures)
    assert any("slice" in failure for failure in failures)
    assert any("forbidden" in failure for failure in failures)
    assert any("http_success_rate" in failure for failure in failures)


def test_gate_rejects_a_baseline_from_the_old_abstention_schema() -> None:
    """hard-only v2 baseline 不得与 hard+soft v3 指标静默比较。"""
    report = {
        "schema_version": 3,
        "artifacts": {"dataset_sha256": "dataset", "snapshot_sha256": "snapshot"},
        "case_count": 1,
        "slice_counts": {"no_answer": 1},
        "metrics": {},
        "slices": {},
        "http_success_rate": 1.0,
    }
    baseline = {
        "schema_version": 2,
        "status": "ready",
        "dataset_sha256": "dataset",
        "snapshot_sha256": "snapshot",
        "case_count": 1,
        "slice_counts": {"no_answer": 1},
        "metrics": {},
        "slices": {},
    }

    failures = check(report, baseline, tolerance=0.01, slice_tolerance=0.05)

    assert failures[0] == "report schema_version 与 baseline 不一致"
