#!/usr/bin/env python
"""仅重跑 glm-5.2，并可将结果替换进既有五模型 benchmark 文件。"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import benchmark_extraction as benchmark


def parse_args() -> argparse.Namespace:
    """解析 glm-5.2 专用重跑参数。"""
    parser = argparse.ArgumentParser(description="仅重跑 glm-5.2 提取 benchmark")
    parser.add_argument(
        "--limit", type=int, choices=range(1, benchmark.NUM_EVENTS + 1), required=True
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--gold", type=Path, help="按 Gold JSONL 中的 event_id 顺序精确选择事件"
    )
    parser.add_argument("--merge-into", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--workers", type=int, choices=range(1, 9), default=1)
    return parser.parse_args()


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    """原子写入 JSONL。"""
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def merge_results(target_path: Path, glm_results: list[dict[str, Any]]) -> None:
    """用新 glm-5.2 行替换既有文件中的旧行，并保持原模型与事件顺序。"""
    existing = [
        json.loads(line)
        for line in target_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    replacement_by_event = {row["event_id"]: row for row in glm_results}
    merged = [
        replacement_by_event[row["event_id"]] if row["model"] == "glm-5.2" else row
        for row in existing
    ]
    expected_replacements = sum(row["model"] == "glm-5.2" for row in existing)
    if expected_replacements != len(glm_results):
        raise RuntimeError(
            f"glm-5.2 替换数量不一致：existing={expected_replacements}, new={len(glm_results)}"
        )
    if len(merged) != len(existing):
        raise RuntimeError("合并后结果行数发生变化")
    write_jsonl(target_path, merged)


def main() -> None:
    """执行 glm-5.2 专用重跑。"""
    args = parse_args()
    keys = benchmark.load_api_keys()
    if not keys["zhipu"]["key"] or not keys["zhipu"]["url"]:
        raise RuntimeError("智谱 LLM_API_KEY 或 LLM_BASE_URL 缺失")
    config = benchmark.get_model_configs(keys)[0]
    if config["model"] != "glm-5.2" or config["provider"] != "zhipu":
        raise RuntimeError(f"glm-5.2 路由错误：{config}")

    full_testset = benchmark.load_or_build_testset()
    if args.gold is not None:
        gold_event_ids = [
            json.loads(line)["event_id"]
            for line in args.gold.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ][: args.limit]
        events_by_id = {event["id"]: event for event in full_testset}
        missing = [
            event_id for event_id in gold_event_ids if event_id not in events_by_id
        ]
        if missing:
            raise RuntimeError(f"Gold 事件不在 benchmark 测试集中：{missing}")
        testset = [events_by_id[event_id] for event_id in gold_event_ids]
    else:
        testset = full_testset[: args.limit]
    fingerprint = benchmark.testset_fingerprint(testset)
    results: list[dict[str, Any]] = []
    if args.resume and args.output.is_file():
        results = [
            json.loads(line)
            for line in args.output.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        results = [result for result in results if result["extraction_error"] is None]
        write_jsonl(args.output, results)
    elif args.output.exists():
        args.output.unlink()
    completed_event_ids = {result["event_id"] for result in results}
    pending_events = [
        event for event in testset if event["id"] not in completed_event_ids
    ]

    def extract_event(event: dict[str, Any]) -> dict[str, Any]:
        extractor = benchmark.make_extractor(config)
        return {
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

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(extract_event, event): event for event in pending_events
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            benchmark.append_jsonl(args.output, result)
            print(
                f"[{len(results)}/{len(testset)}] event={result['event_id']} "
                f"claims={result['claims_count']} error={result['extraction_error']}"
            )
    if len(results) != len(testset):
        raise RuntimeError(
            f"glm-5.2 结果数量错误：expected={len(testset)}, actual={len(results)}"
        )
    event_order = {event["id"]: index for index, event in enumerate(testset)}
    results.sort(key=lambda result: event_order[result["event_id"]])
    write_jsonl(args.output, results)
    errors = [result for result in results if result["extraction_error"]]
    if errors:
        raise RuntimeError(f"glm-5.2 重跑存在 {len(errors)} 个错误，未执行合并")
    if args.merge_into is not None:
        merge_results(args.merge_into, results)


if __name__ == "__main__":
    main()
