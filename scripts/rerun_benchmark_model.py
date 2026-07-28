#!/usr/bin/env python
"""重跑单个 extraction benchmark 模型，并可原子替换合并结果。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import benchmark_extraction as benchmark


def parse_args() -> argparse.Namespace:
    """解析模型、输出和可选合并目标。"""
    parser = argparse.ArgumentParser(description="重跑指定 extraction benchmark 模型")
    parser.add_argument("--model", choices=benchmark.MODELS, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--merge-into", type=Path)
    return parser.parse_args()


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    """原子写入 JSONL。"""
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def merge_results(target_path: Path, model: str, replacements: list[dict[str, Any]]) -> None:
    """用指定模型的新结果替换五模型产物中的同模型行。"""
    existing = [json.loads(line) for line in target_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    replacement_by_event = {row["event_id"]: row for row in replacements}
    expected = sum(row["model"] == model for row in existing)
    if expected != len(replacement_by_event):
        raise RuntimeError(f"{model} 替换数量不一致：existing={expected}, new={len(replacement_by_event)}")
    merged = [replacement_by_event[row["event_id"]] if row["model"] == model else row for row in existing]
    write_jsonl(target_path, merged)


def main() -> None:
    """串行重跑单模型的全部固定测试事件。"""
    args = parse_args()
    keys = benchmark.load_api_keys()
    benchmark.validate_credentials(keys)
    config = next(config for config in benchmark.get_model_configs(keys) if config["model"] == args.model)
    testset = benchmark.load_or_build_testset()
    fingerprint = benchmark.testset_fingerprint(testset)
    results: list[dict[str, Any]] = []
    for index, event in enumerate(testset, start=1):
        extractor = benchmark.make_extractor(config)
        result = {
            "model": config["model"],
            "provider": config["provider"],
            "enable_thinking": config["enable_thinking"],
            "event_id": event["id"],
            "category": event["category"],
            "actor": event["actor_type"],
            **benchmark.run_single_extraction(
                extractor,
                event["content"],
                {
                    "session_id": event["session_id"],
                    "actor": event["actor_type"],
                    "actor_type": event["actor_type"],
                    "source_kind": event["category"],
                },
            ),
            "testset_fingerprint": fingerprint,
        }
        results.append(result)
        benchmark.append_jsonl(args.output, result)
        if index % 10 == 0 or index == len(testset):
            errors = sum(item["extraction_error"] is not None for item in results)
            print(f"[{index}/{len(testset)}] ok={index - errors} err={errors}", flush=True)

    write_jsonl(args.output, results)
    if args.merge_into is not None:
        merge_results(args.merge_into, args.model, results)


if __name__ == "__main__":
    main()
