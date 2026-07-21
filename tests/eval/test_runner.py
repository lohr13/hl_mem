"""评测 runner 测试。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from tests.eval.dataset import EvalCase
from tests.eval.runner import run_evaluation, write_report


def test_runner_records_per_case_metrics_and_manifest(tmp_path: Path) -> None:
    database_path = tmp_path / "snapshot.db"
    sqlite3.connect(database_path).close()
    case = EvalCase(
        case_id="N01", query="不存在？", intent="current_state", expected_type="empty",
        expected_min_confidence=None, expected_status_filter="active", expected_keywords=(), keyword_match="all",
        binding=None, forbidden_statuses=("disputed",),
    )

    report = run_evaluation([case], lambda _case: {"results": [], "observations": []}, database_path)
    output = tmp_path / "report.json"
    write_report(report, output)

    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert persisted["manifest"]["source_sha256"]
    assert persisted["manifest"]["case_count"] == 1
    assert persisted["queries"][0]["case_id"] == "N01"
    assert persisted["metrics"]["no_answer_recall"] == 1.0
