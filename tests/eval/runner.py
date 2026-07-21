"""可复用的离线召回评测 runner 与命令行入口。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi.testclient import TestClient

from hl_mem.api.server import create_app
from tests.eval.dataset import EvalCase, bind_cases, load_cases
from tests.eval.metrics import aggregate_metrics, evaluate_results


RecallCallable = Callable[[EvalCase], dict[str, Any]]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_evaluation(cases: list[EvalCase], recall: RecallCallable, source_path: str | Path) -> dict[str, Any]:
    """运行全部样本并返回含 manifest、逐条诊断和聚合指标的报告。"""
    source = Path(source_path).resolve()
    scores = []
    queries = []
    for case in cases:
        started = time.perf_counter()
        response = recall(case)
        latency_ms = (time.perf_counter() - started) * 1000
        score = evaluate_results(case, response, latency_ms)
        scores.append(score)
        queries.append({"case_id": case.case_id, "response": response, "score": score.as_dict()})
    return {
        "manifest": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_path": str(source),
            "source_sha256": _sha256(source),
            "case_count": len(cases),
            "real_api": os.getenv("HL_MEM_EVAL_REAL_API") == "1",
        },
        "metrics": aggregate_metrics(scores),
        "queries": queries,
    }


def write_report(report: dict[str, Any], path: str | Path) -> None:
    """原子式写入 UTF-8 JSON 评测报告。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(target)


def _main() -> int:
    parser = argparse.ArgumentParser(description="运行 HL-Mem recall_v2 离线评测")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--dataset", type=Path, default=Path(__file__).parent / "datasets" / "recall_v2.jsonl")
    parser.add_argument("--report", required=True, type=Path)
    arguments = parser.parse_args()
    uri = f"file:{arguments.database.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    cases = bind_cases(connection, load_cases(arguments.dataset))
    connection.close()
    with tempfile.TemporaryDirectory(prefix="hl-mem-eval-") as temporary_directory:
        working_database = Path(temporary_directory) / "working.db"
        shutil.copy2(arguments.database, working_database)
        app = create_app(working_database)
        with TestClient(app) as client:
            report = run_evaluation(
                cases,
                lambda case: client.post(
                    "/v1/recall",
                    json={
                        "query": case.query,
                        "limit": 5,
                        "intent": case.intent,
                        "as_of": case.as_of,
                        "known_as_of": case.known_as_of,
                    },
                ).json(),
                arguments.database,
            )
    write_report(report, arguments.report)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
