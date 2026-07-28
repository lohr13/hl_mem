#!/usr/bin/env python
"""对五个模型执行结构化记忆提取 benchmark。"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from hl_mem.ingest.chunking import ChunkingPolicy
from hl_mem.ingest.llm_extractor import LLMExtractor
from hl_mem.llm.client import LLMClient
from hl_mem.llm.providers import DashScopeProvider
from hl_mem.llm.types import StructuredOutputMode
from hl_mem.storage._shared import decode_json

DB_PATH = PROJECT_ROOT / "var" / "hl_mem.db"
TESTSET_PATH = PROJECT_ROOT / "scripts" / "extraction_testset.jsonl"
RESULTS_PATH = PROJECT_ROOT / "scripts" / "extraction_benchmark_results.jsonl"
NUM_EVENTS = 50
MODELS = ("glm-5.2", "glm-5", "glm-4.7", "qwen3.7-plus", "qwen3.6-plus")


def load_api_keys() -> dict[str, dict[str, str | None]]:
    """从环境变量或 .env 加载百炼凭据。"""
    dashscope: dict[str, str | None] = {
        "key": os.getenv("LLM_API_KEY"),
        "url": os.getenv("LLM_BASE_URL"),
    }
    env_path = PROJECT_ROOT / ".env"
    if env_path.is_file():
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if dashscope["key"] is None and line.startswith("LLM_API_KEY="):
                dashscope["key"] = line.split("=", 1)[1].strip()
            elif dashscope["url"] is None and line.startswith("LLM_BASE_URL="):
                dashscope["url"] = line.split("=", 1)[1].strip()
    dashscope["url"] = dashscope["url"] or "https://coding.dashscope.aliyuncs.com/v1"
    return {"dashscope": dashscope}


def build_testset() -> list[dict[str, Any]]:
    """从数据库按事件类型采样 benchmark 测试集。"""
    connection = sqlite3.connect(str(DB_PATH))
    connection.row_factory = sqlite3.Row
    events: list[dict[str, Any]] = []
    queries = [
        ("user_pref", "actor_type='user'", 10),
        ("project_config", "content_json LIKE '%hl_mem%' OR content_json LIKE '%config%'", 10),
        ("tool_workflow", "actor_type='tool' OR content_json LIKE '%Codex%' OR content_json LIKE '%pytest%'", 10),
        ("status_report", "content_json LIKE '%healthz%' OR content_json LIKE '%status%'", 5),
        ("chat_confirm", "actor_type='assistant' AND LENGTH(content_json) < 300", 5),
        ("long_content", "LENGTH(content_json) > 2000", 10),
    ]

    seen_ids: set[str] = set()
    for category, where_clause, limit in queries:
        if seen_ids:
            placeholders = ",".join("?" * len(seen_ids))
            rows = connection.execute(
                f"SELECT * FROM events WHERE {where_clause} AND id NOT IN ({placeholders}) ORDER BY RANDOM() LIMIT ?",
                (*seen_ids, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                f"SELECT * FROM events WHERE {where_clause} ORDER BY RANDOM() LIMIT ?",
                (limit,),
            ).fetchall()
        for row in rows:
            if row["id"] in seen_ids:
                continue
            content = decode_json(row["content_json"])
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            events.append(
                {
                    "id": row["id"],
                    "category": category,
                    "actor_type": row["actor_type"],
                    "session_id": row["session_id"] or "",
                    "content": text[:2000],
                    "content_length": len(text),
                }
            )
            seen_ids.add(row["id"])

    remaining = NUM_EVENTS - len(events)
    if remaining > 0:
        placeholders = ",".join("?" * len(seen_ids))
        rows = connection.execute(
            f"SELECT * FROM events WHERE id NOT IN ({placeholders}) ORDER BY RANDOM() LIMIT ?",
            (*seen_ids, remaining),
        ).fetchall()
        for row in rows:
            content = decode_json(row["content_json"])
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            events.append(
                {
                    "id": row["id"],
                    "category": "random_fill",
                    "actor_type": row["actor_type"],
                    "session_id": row["session_id"] or "",
                    "content": text[:2000],
                    "content_length": len(text),
                }
            )
    connection.close()
    return events[:NUM_EVENTS]


def load_or_build_testset() -> list[dict[str, Any]]:
    """复用完整测试集；数量不符时重新采样并落盘。"""
    if TESTSET_PATH.is_file():
        testset = [json.loads(line) for line in TESTSET_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
        if len(testset) == NUM_EVENTS:
            return testset

    testset = build_testset()
    TESTSET_PATH.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in testset) + "\n",
        encoding="utf-8",
    )
    return testset


def get_model_configs(keys: dict[str, dict[str, str | None]]) -> list[dict[str, Any]]:
    """返回全部使用百炼端点的五模型配置。"""
    credentials = keys["dashscope"]
    return [
        {
            "name": model,
            "provider": "dashscope",
            "model": model,
            "api_key": credentials["key"],
            "base_url": credentials["url"],
            "enable_thinking": False,
        }
        for model in MODELS
    ]


def make_extractor(config: dict[str, Any]) -> LLMExtractor:
    """按模型配置构造使用统一提取策略的 extractor。"""
    provider = DashScopeProvider(enable_thinking=False)

    client = LLMClient(
        api_key=config["api_key"],
        base_url=config["base_url"],
        model=config["model"],
        provider=provider,
        timeout=httpx.Timeout(120.0, connect=15.0),
        max_attempts=2,
        operation="benchmark",
    )
    return LLMExtractor(
        llm_client=client,
        schema_retries=2,
        structured_mode=StructuredOutputMode.JSON_OBJECT,
        chunking_policy=ChunkingPolicy(target_chars=12000, overlap_turns=2, max_split_depth=3),
    )


def run_single_extraction(extractor: LLMExtractor, content: str, context: dict[str, Any]) -> dict[str, Any]:
    """运行一次提取，并保留结构化质量、用量及 HTTP 错误指标。"""
    started = time.perf_counter()
    metrics: dict[str, Any] = {
        "schema_retry_count": 0,
        "repair_count": 0,
        "llm_call_count": 0,
        "should_memorize": False,
        "claims_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "extraction_error": None,
        "http_status_code": None,
        "http_response_body": None,
        "claims_data": [],
    }
    try:
        extractor._schema_retry_count = 0
        extractor._repair_count = 0
        extractor._llm_call_count = 0
        extractor._memorize_decisions = []
        claims = extractor.extract(content, context)
        metrics.update(
            {
                "schema_retry_count": getattr(extractor, "_schema_retry_count", 0),
                "repair_count": getattr(extractor, "_repair_count", 0),
                "llm_call_count": getattr(extractor, "_llm_call_count", 0),
                "should_memorize": bool(claims),
                "claims_count": len(claims),
                "input_tokens": extractor.last_input_tokens,
                "output_tokens": extractor.last_output_tokens,
                "total_tokens": extractor.last_usage_tokens,
                "claims_data": [
                    {
                        "subject": claim.subject,
                        "predicate": claim.predicate,
                        "scope": claim.scope,
                        "importance": claim.importance,
                    }
                    for claim in claims
                ],
            }
        )
    except httpx.HTTPStatusError as error:
        metrics["extraction_error"] = f"{type(error).__name__}: {str(error)[:200]}"
        metrics["http_status_code"] = error.response.status_code
        metrics["http_response_body"] = error.response.text[:500]
    except Exception as error:
        metrics["extraction_error"] = f"{type(error).__name__}: {str(error)[:200]}"
    metrics["latency_ms"] = round((time.perf_counter() - started) * 1000)
    return metrics


def print_summary(results: list[dict[str, Any]]) -> None:
    """打印五模型核心指标对比。"""
    stats: dict[str, dict[str, Any]] = {}
    for model in MODELS:
        model_results = [result for result in results if result["model"] == model]
        successes = [result for result in model_results if result["extraction_error"] is None]
        claims = [claim for result in successes for claim in result.get("claims_data", [])]
        predicates = Counter(claim["predicate"] for claim in claims)
        stats[model] = {
            "total": len(model_results),
            "errors": len(model_results) - len(successes),
            "schema_success_rate": len(successes) / max(len(model_results), 1),
            "repairs": sum(result["repair_count"] for result in successes),
            "claim_count": len(claims),
            "avg_tokens": sum(result["total_tokens"] for result in successes) / max(len(successes), 1),
            "avg_latency": sum(result["latency_ms"] for result in successes) / max(len(successes), 1),
            "subject_diversity": len({claim["subject"] for claim in claims}),
            "top_predicates": predicates.most_common(3),
        }

    print(f"\n{'指标':<22}", end="")
    for model in MODELS:
        print(f" | {model:>14}", end="")
    print()
    rows = (
        ("总调用数", "total", "d"),
        ("错误数", "errors", "d"),
        ("schema 成功率", "schema_success_rate", ".1%"),
        ("repair 次数", "repairs", "d"),
        ("总 claims 数", "claim_count", "d"),
        ("avg total tokens", "avg_tokens", ".0f"),
        ("avg latency (ms)", "avg_latency", ".0f"),
        ("subject diversity", "subject_diversity", "d"),
    )
    for label, key, value_format in rows:
        print(f"{label:<22}", end="")
        for model in MODELS:
            value = stats[model][key]
            if value_format == "d":
                print(f" | {int(value):>14}", end="")
            elif value_format == ".1%":
                print(f" | {value:>13.1%}", end="")
            else:
                print(f" | {value:>14{value_format}}", end="")
        print()
    print("\nTop-3 predicate:")
    for model in MODELS:
        print(f"  {model}: {', '.join(f'{name}({count})' for name, count in stats[model]['top_predicates'])}")


def main() -> None:
    """执行完整五模型 benchmark，并将结果写入时间戳文件。"""
    print("=" * 70)
    print("  hl_mem 提取质量横评 — 5 模型对比")
    print("=" * 70)
    keys = load_api_keys()
    configs = get_model_configs(keys)
    missing = [config["provider"] for config in configs if not config["api_key"] or not config["base_url"]]
    if missing:
        raise RuntimeError(f"以下 provider 缺少凭据: {sorted(set(missing))}")
    print("\n凭据: dashscope=✓")
    print(f"模型: {[config['model'] for config in configs]}")

    testset = load_or_build_testset()
    print(f"\n[1/3] 测试集: {len(testset)} 条")
    for category, count in Counter(event["category"] for event in testset).most_common():
        print(f"  {category}: {count}")

    RESULTS_PATH.unlink(missing_ok=True)
    results_path = RESULTS_PATH
    results: list[dict[str, Any]] = []
    print(f"\n[2/3] 运行 {len(testset)} events × {len(configs)} models")
    for config in configs:
        print(f"\n  ── {config['model']} ──")
        extractor = make_extractor(config)
        for index, event in enumerate(testset, start=1):
            result = {
                "model": config["model"],
                "provider": config["provider"],
                "enable_thinking": config["enable_thinking"],
                "event_id": event["id"],
                "category": event["category"],
                "actor": event["actor_type"],
                **run_single_extraction(
                    extractor,
                    event["content"],
                    {"session_id": event["session_id"], "actor": event["actor_type"]},
                ),
            }
            results.append(result)
            with results_path.open("a", encoding="utf-8") as output:
                output.write(json.dumps(result, ensure_ascii=False) + "\n")
            if index % 10 == 0 or index == len(testset):
                model_results = [item for item in results if item["model"] == config["model"]]
                errors = sum(item["extraction_error"] is not None for item in model_results)
                print(f"    [{index}/{len(testset)}] ok={len(model_results) - errors} err={errors}")

    print("\n[3/3] 汇总对比")
    print_summary(results)
    print(f"\n原始结果: {results_path}")
    print(f"测试集: {TESTSET_PATH}")


if __name__ == "__main__":
    main()
