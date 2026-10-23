"""Case-weighted merging of independently materialized dataset shards."""

import json
from pathlib import Path

import pytest

from evaluation.tools import merge_longmemeval_results as merger
from evaluation.tools import run_longmemeval_benchmark as runner
from tests.unit.test_longmemeval_batching import _case_result, _record, _shard_report


def test_expansion_usage_reports_execution_separately_from_model_label():
    cases = [_case_result("a"), _case_result("b"), _case_result("legacy")]
    cases[0]["retrieval"]["search_trace"] = {
        "expansions": [{"expansion_text": "related query"}],
        "expansion_total_tokens": 23,
    }
    cases[1]["retrieval"]["search_trace"] = {"expansions": [], "expansion_total_tokens": 0}
    summary = runner.aggregate_results(cases)["overall"]
    assert summary["query_expansion_traced_cases"] == 2
    assert summary["query_expansion_executed_cases"] == 1
    assert summary["query_expansion_total_tokens"] == 23


def _reports(root: Path) -> Path:
    records = [_record(f"sample-{i}") for i in range(5)]
    dataset = root / "dataset.json"
    dataset.write_text(json.dumps(records), encoding="utf-8")
    for index, subset in enumerate((records[:1], records[1:])):
        shard = root / f"data-{index}.json"
        shard.write_text(json.dumps(subset), encoding="utf-8")
        cases = [_case_result(record["question_id"], correct=index == 0) for record in subset]
        for case in cases:
            source = runner.normalize_case(
                next(record for record in subset if record["question_id"] == case["case_id"])
            )
            case.update(
                question=source.question,
                answer=source.answer,
                question_at=source.question_at,
                gold_session_ids=list(source.gold_session_ids),
            )
            case["retrieval"]["eligible"] = True
            case["retrieval"]["recall_at_10"] = float(index == 0)
        if index == 1:
            cases[-1]["retrieval"].update(eligible=False, recall_at_10=None)
            cases[-1]["evaluation_eligibility"] = {"temporal_gate_eligible": False}
        report = _shard_report(runner._file_sha256(shard), cases)
        report["dataset"]["path"] = str(shard)
        report["metrics"] = {"overall": {"recall_at_10": 999}}
        (root / f"shard-{index}.json").write_text(json.dumps(report), encoding="utf-8")
    return dataset


def test_partition_merge_recomputes_case_mean_and_keeps_raw_gate(tmp_path):
    dataset = _reports(tmp_path)
    report = merger.merge_reports(
        input_dir=tmp_path, pattern="shard-*.json", dataset=dataset, partitioned_datasets=True
    )
    overall = report["metrics"]["overall"]
    assert overall["recall_at_10"] == 0.25  # shard mean would be 0.5
    assert overall["recall_at_10_sum"] == 1.0
    assert overall["recall_at_10_count"] == 4
    assert overall["qa_accuracy"] == 0.2
    assert overall["gate_qa_accuracy"] == 0.25
    assert overall["cases"] == 5
    assert overall["gate_excluded_cases"] == 1
    assert len(report["run"]["merge"]["source_datasets"]) == 2


@pytest.mark.parametrize(
    "tamper",
    [
        "hash",
        "content",
        "query_expansion_model",
        "extractor_base_url",
        "query_expansion_mode",
        "question",
        "answer",
        "gold_session_ids",
    ],
)
def test_partition_merge_rejects_unverified_identity(tmp_path, tamper):
    dataset = _reports(tmp_path)
    path = tmp_path / "shard-1.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    if tamper in {"hash", "content"}:
        shard = Path(report["dataset"]["path"])
        records = json.loads(shard.read_text(encoding="utf-8"))
        records[0]["question"] = "Changed question"
        shard.write_text(json.dumps(records), encoding="utf-8")
        if tamper == "content":
            report["dataset"]["sha256"] = runner._file_sha256(shard)
    elif tamper in {"question", "answer", "gold_session_ids"}:
        report["cases"][0][tamper] = "wrong"
    else:
        report["run"]["models"][tamper] = "changed"
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="mismatch"):
        merger.merge_reports(input_dir=tmp_path, pattern="shard-*.json", dataset=dataset, partitioned_datasets=True)
