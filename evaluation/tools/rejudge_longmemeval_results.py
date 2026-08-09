#!/usr/bin/env python
"""Rejudge existing LongMemEval predicted answers without ingesting or retrieving."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.tools import run_longmemeval_benchmark as runner  # noqa: E402
from hl_mem.config_loader import load_settings  # noqa: E402

Judge = Callable[[dict[str, Any]], tuple[dict[str, Any], int]]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path, help="existing LongMemEval shard result JSON files")
    parser.add_argument("--config", type=Path, default=runner.DEFAULT_CONFIG)
    parser.add_argument("--env-file", type=Path, default=runner.DEFAULT_ENV_FILE)
    parser.add_argument("--model", help="judge model; defaults to HL_MEM_EVAL_QA_MODEL or the runner QA model")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def _read_cases(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"LongMemEval result must contain a JSON object: {path}")
    if payload.get("benchmark") != "LongMemEval-S":
        raise ValueError(f"not a LongMemEval-S result: {path}")
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list):
        raise ValueError(f"LongMemEval result is missing a cases array: {path}")
    cases: list[dict[str, Any]] = []
    for raw_case in raw_cases:
        if not isinstance(raw_case, dict):
            raise ValueError(f"LongMemEval result contains a non-object case: {path}")
        cases.append(raw_case)
    return cases


def _required_case_text(case: Mapping[str, Any], field: str, source: Path) -> str:
    value = case.get(field)
    if not isinstance(value, str) or not value.strip():
        case_id = case.get("case_id") or "<unknown>"
        raise ValueError(f"case {case_id!r} is missing string field {field!r}: {source}")
    return value


def _change_label(old_correct: bool, new_correct: bool) -> str:
    if old_correct and not new_correct:
        return "correct_to_wrong"
    if not old_correct and new_correct:
        return "wrong_to_correct"
    return "unchanged_correct" if new_correct else "unchanged_wrong"


def rejudge_inputs(inputs: Sequence[Path], *, judge: Judge, model: str) -> list[dict[str, Any]]:
    """Load result shards and compare their existing QA verdicts with a new judge."""
    comparisons: list[dict[str, Any]] = []
    seen_case_ids: dict[str, Path] = {}
    for source in inputs:
        for case in _read_cases(source):
            case_id = _required_case_text(case, "case_id", source)
            previous_source = seen_case_ids.get(case_id)
            if previous_source is not None:
                raise ValueError(f"duplicate case_id {case_id!r} in {previous_source} and {source}")
            seen_case_ids[case_id] = source
            question_type = _required_case_text(case, "question_type", source)
            question = _required_case_text(case, "question", source)
            answer = _required_case_text(case, "answer", source)
            base = {
                "case_id": case_id,
                "question_type": question_type,
                "question": question,
                "answer": answer,
                "source_file": str(source),
            }
            qa = case.get("qa")
            if not isinstance(qa, Mapping) or not isinstance(qa.get("predicted_answer"), str):
                comparisons.append(
                    {
                        **base,
                        "predicted_answer": None,
                        "old_correct": None,
                        "old_reason": None,
                        "new_correct": None,
                        "new_reason": None,
                        "judge_model": model,
                        "judge_tokens": 0,
                        "change": "skipped",
                        "skip_reason": "existing case has no predicted_answer",
                        "existing_error": case.get("error"),
                    }
                )
                continue
            old_correct = qa.get("correct")
            if not isinstance(old_correct, bool):
                raise ValueError(f"case {case_id!r} QA result is missing boolean 'correct': {source}")
            predicted_answer = str(qa["predicted_answer"])
            judgment, judge_tokens = judge(case)
            new_correct = judgment.get("correct")
            if not isinstance(new_correct, bool):
                raise ValueError(f"case {case_id!r} new judge result is missing boolean 'correct'")
            comparisons.append(
                {
                    **base,
                    "predicted_answer": predicted_answer,
                    "old_correct": old_correct,
                    "old_reason": str(qa.get("reason") or ""),
                    "new_correct": new_correct,
                    "new_reason": str(judgment.get("reason") or ""),
                    "judge_model": model,
                    "judge_tokens": judge_tokens,
                    "change": _change_label(old_correct, new_correct),
                    "skip_reason": None,
                    "existing_error": case.get("error"),
                }
            )
    return comparisons


def summarize_comparisons(comparisons: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    evaluated = [
        item
        for item in comparisons
        if isinstance(item.get("old_correct"), bool) and isinstance(item.get("new_correct"), bool)
    ]
    old_correct = sum(bool(item["old_correct"]) for item in evaluated)
    new_correct = sum(bool(item["new_correct"]) for item in evaluated)
    denominator = len(evaluated)
    old_accuracy = old_correct / denominator if denominator else None
    new_accuracy = new_correct / denominator if denominator else None
    return {
        "total_cases": len(comparisons),
        "evaluated_cases": denominator,
        "skipped_cases": len(comparisons) - denominator,
        "old_correct": old_correct,
        "new_correct": new_correct,
        "old_accuracy": old_accuracy,
        "new_accuracy": new_accuracy,
        "accuracy_delta": (
            new_accuracy - old_accuracy if new_accuracy is not None and old_accuracy is not None else None
        ),
        "wrong_to_correct": sum(item.get("change") == "wrong_to_correct" for item in evaluated),
        "correct_to_wrong": sum(item.get("change") == "correct_to_wrong" for item in evaluated),
        "unchanged_correct": sum(item.get("change") == "unchanged_correct" for item in evaluated),
        "unchanged_wrong": sum(item.get("change") == "unchanged_wrong" for item in evaluated),
        "judge_tokens": sum(int(item.get("judge_tokens") or 0) for item in comparisons),
    }


def _report(inputs: Sequence[Path], model: str, comparisons: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "benchmark": "LongMemEval-S-rejudge",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "judge": {
            "model": model,
            "prompt_version": runner.LONGMEMEVAL_JUDGE_PROMPT_VERSION,
            "source": "LongMemEval official evaluate_qa.py-compatible rules",
        },
        "inputs": [
            {
                "path": str(path.resolve()),
                "sha256": runner._file_sha256(path),
            }
            for path in inputs
        ],
        "summary": summarize_comparisons(comparisons),
        "cases": list(comparisons),
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    settings = load_settings(args.config, args.env_file)
    api_key = os.environ.get("LLM_API_KEY") or settings.llm_api_key
    if not api_key:
        raise RuntimeError("rejudging requires LLM_API_KEY in .env or environment")
    model = str(args.model or runner._qa_model(settings))
    total = sum(
        isinstance(case.get("qa"), Mapping) and isinstance(case["qa"].get("predicted_answer"), str)
        for path in args.inputs
        for case in _read_cases(path)
    )
    completed = 0

    def judge(case: dict[str, Any]) -> tuple[dict[str, Any], int]:
        nonlocal completed
        completed += 1
        case_id = str(case["case_id"])
        print(f"[{completed}/{total}] judging {case_id}", flush=True)
        qa = case["qa"]
        if not isinstance(qa, Mapping) or not isinstance(qa.get("predicted_answer"), str):
            raise ValueError(f"case {case_id!r} has no predicted answer")
        return runner._judge_longmemeval_answer(
            api_key=str(api_key),
            base_url=settings.llm_base_url,
            model=model,
            case_id=case_id,
            question_type=str(case["question_type"]),
            question=str(case["question"]),
            answer=str(case["answer"]),
            predicted_answer=str(qa["predicted_answer"]),
        )

    comparisons = rejudge_inputs(args.inputs, judge=judge, model=model)
    report = _report(args.inputs, model, comparisons)
    runner._write_json_atomic(args.output, report)
    summary = report["summary"]
    print(
        f"rejudged evaluated={summary['evaluated_cases']} skipped={summary['skipped_cases']} "
        f"old={summary['old_correct']}/{summary['evaluated_cases']} ({summary['old_accuracy']:.4f}) "
        f"new={summary['new_correct']}/{summary['evaluated_cases']} ({summary['new_accuracy']:.4f}) "
        f"delta={summary['accuracy_delta']:+.4f} output={args.output}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
