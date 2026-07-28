#!/usr/bin/env python
"""对五个模型执行结构化记忆提取 benchmark。"""

from __future__ import annotations

import argparse
import hashlib
import json
import msvcrt
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from hl_mem.ingest.chunking import ChunkingPolicy
from hl_mem.ingest.llm_extractor import LLMExtractor
from hl_mem.llm.client import LLMClient
from hl_mem.llm.providers import DashScopeProvider, ZhipuProvider
from hl_mem.llm.types import StructuredOutputMode
from hl_mem.storage._shared import decode_json

DB_PATH = PROJECT_ROOT / "var" / "hl_mem.db"
TESTSET_PATH = PROJECT_ROOT / "scripts" / "extraction_testset.jsonl"
RESULTS_PATH = PROJECT_ROOT / "scripts" / "extraction_benchmark_results.jsonl"
RUNS_ROOT = PROJECT_ROOT / "scripts" / "benchmark_runs"
LOCK_PATH = PROJECT_ROOT / "scripts" / ".benchmark_extraction.lock"
HERMES_CONFIG_PATH = Path(
    os.getenv("HERMES_CONFIG_PATH", str(Path(os.environ["LOCALAPPDATA"]) / "hermes" / "config.yaml"))
)
NUM_EVENTS = 50
VALIDATION_EVENTS = 3
MODELS = ("glm-5.2", "glm-5", "glm-4.7", "qwen3.7-plus", "qwen3.6-plus")
RUN_FILE_NAMES = ("manifest.json", "testset.jsonl", "results.partial.jsonl", "results.jsonl")


@contextmanager
def exclusive_run_lock(path: Path = LOCK_PATH) -> Iterator[None]:
    """使用 Windows 文件锁阻止 benchmark 并发运行。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = path.open("a+", encoding="utf-8")
    try:
        lock_file.seek(0)
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise RuntimeError(f"已有 benchmark 进程持有独占锁：{path}") from error
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(f"pid={os.getpid()} started_at={datetime.now(timezone.utc).isoformat()}\n")
        lock_file.flush()
        yield
    finally:
        try:
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            lock_file.close()


def prepare_run_paths(mode: str, *, resume: bool) -> dict[str, Path]:
    """隔离 validation/full 产物，新运行开始前清理该模式全部旧文件。"""
    run_dir = RUNS_ROOT / mode
    run_dir.mkdir(parents=True, exist_ok=True)
    paths = {name: run_dir / name for name in RUN_FILE_NAMES}
    if resume:
        if not paths["manifest.json"].is_file():
            raise RuntimeError(f"无法续跑：缺少 {paths['manifest.json']}")
    else:
        for path in paths.values():
            path.unlink(missing_ok=True)
    return paths


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """原子写入 JSON 文件。"""
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary_path.replace(path)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    """追加一条结果并强制刷盘，以便失败后续跑。"""
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(payload, ensure_ascii=False) + "\n")
        output.flush()
        os.fsync(output.fileno())


def load_api_keys() -> dict[str, dict[str, str | None]]:
    """从环境变量或 .env 加载智谱凭据，并从 Hermes 配置加载百炼凭据。"""
    zhipu: dict[str, str | None] = {
        "key": os.getenv("LLM_API_KEY"),
        "url": os.getenv("LLM_BASE_URL"),
    }
    env_path = PROJECT_ROOT / ".env"
    if env_path.is_file():
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if zhipu["key"] is None and line.startswith("LLM_API_KEY="):
                zhipu["key"] = line.split("=", 1)[1].strip()
            elif zhipu["url"] is None and line.startswith("LLM_BASE_URL="):
                zhipu["url"] = line.split("=", 1)[1].strip()
    return {"zhipu": zhipu, "dashscope": read_hermes_dashscope_config(HERMES_CONFIG_PATH)}


def read_hermes_dashscope_config(path: Path) -> dict[str, str | None]:
    """从 Hermes YAML 配置的 providers.dashscope 节读取百炼凭据。"""
    credentials: dict[str, str | None] = {"key": None, "url": None}
    if not path.is_file():
        return credentials

    in_providers = False
    in_dashscope = False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip())
        if indent == 0:
            in_providers = stripped == "providers:"
            in_dashscope = False
        elif in_providers and indent == 2:
            in_dashscope = stripped == "dashscope:"
        elif in_dashscope and indent == 4 and ":" in stripped:
            name, value = stripped.split(":", 1)
            normalized_value = value.strip().strip("'\"")
            if name == "api_key":
                credentials["key"] = normalized_value
            elif name == "base_url":
                credentials["url"] = normalized_value
    return credentials


def validate_credentials(keys: dict[str, dict[str, str | None]]) -> None:
    """拒绝缺失凭据或错误的 provider endpoint。"""
    dashscope_key = keys["dashscope"]["key"]
    dashscope_url = keys["dashscope"]["url"]
    zhipu_key = keys["zhipu"]["key"]
    zhipu_url = keys["zhipu"]["url"]
    if not dashscope_key or not dashscope_key.startswith("sk-sp"):
        raise RuntimeError("LLM_API_KEY 缺失或不是 sk-sp 百炼 Coding Plan key")
    if not dashscope_url:
        raise RuntimeError("百炼 LLM_BASE_URL 缺失")
    dashscope_host = urlparse(dashscope_url).netloc
    if dashscope_host != "coding.dashscope.aliyuncs.com":
        raise RuntimeError(f"百炼端点错误：host={dashscope_host!r}")
    if not zhipu_key or not zhipu_url:
        raise RuntimeError("智谱 LLM_API_KEY 或 LLM_BASE_URL 缺失")
    zhipu_host = urlparse(zhipu_url).netloc
    if zhipu_host != "open.bigmodel.cn":
        raise RuntimeError(f"智谱端点错误：host={zhipu_host!r}")


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
    """返回 glm-5.2 走智谱、其余四模型走百炼的配置。"""
    return [
        {
            "name": model,
            "provider": "zhipu" if model == "glm-5.2" else "dashscope",
            "model": model,
            "api_key": keys["zhipu" if model == "glm-5.2" else "dashscope"]["key"],
            "base_url": keys["zhipu" if model == "glm-5.2" else "dashscope"]["url"],
            "enable_thinking": False,
        }
        for model in MODELS
    ]


def make_extractor(config: dict[str, Any]) -> LLMExtractor:
    """按模型配置构造使用统一提取策略的 extractor。"""
    provider = (
        ZhipuProvider()
        if config["provider"] == "zhipu"
        else DashScopeProvider(enable_thinking=config["enable_thinking"])
    )

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
        "schema_error_paths": None,
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
                        "value": claim.value,
                        "scope": claim.scope,
                        "importance": claim.importance,
                        "canonical_attribute": claim.canonical_attribute,
                        "canonical_slot": claim.canonical_slot,
                        "topic_tags": claim.topic_tags,
                        "confidence": claim.confidence,
                        "volatility": claim.volatility,
                        "qualifiers": claim.qualifiers,
                        "reason": claim.reason,
                    }
                    for claim in claims
                ],
            }
        )
    except Exception as error:
        metrics["extraction_error"] = f"{type(error).__name__}: {str(error)[:200]}"
        http_error = find_http_status_error(error)
        if http_error is not None:
            metrics["http_status_code"] = http_error.response.status_code
            metrics["http_response_body"] = sanitize_http_response_body(http_error.response.text)
    schema_error_paths = [
        ".".join(str(part) for part in error.get("loc", ()))
        for error in getattr(extractor, "_last_schema_errors", [])
        if error.get("loc")
    ]
    metrics["schema_error_paths"] = schema_error_paths or None
    metrics["latency_ms"] = round((time.perf_counter() - started) * 1000)
    return metrics


def sanitize_http_response_body(body: str) -> str:
    """脱敏并截断 HTTP 错误响应，避免 benchmark 产物泄露凭据。"""
    sanitized = re.sub(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?bearer\s+)[^\s,\"']+", r"\1[REDACTED]", body)
    sanitized = re.sub(
        r"(?i)((?:api[_-]?key|access[_-]?token|secret)[\"']?\s*[:=]\s*[\"']?)[^\s,\"']+",
        r"\1[REDACTED]",
        sanitized,
    )
    sanitized = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "sk-[REDACTED]", sanitized)
    return sanitized[:500]


def find_http_status_error(error: BaseException) -> httpx.HTTPStatusError | None:
    """沿异常因果链提取被包装的 HTTPStatusError。"""
    visited: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in visited:
        if isinstance(current, httpx.HTTPStatusError):
            return current
        visited.add(id(current))
        current = current.__cause__ or current.__context__
    return None


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


def parse_args() -> argparse.Namespace:
    """解析运行模式；validation 必须先于 full 人工执行。"""
    parser = argparse.ArgumentParser(description="hl_mem 五模型结构化提取 benchmark")
    parser.add_argument("--mode", choices=("validation", "full"), required=True)
    parser.add_argument("--resume", action="store_true", help="保留同一 run 已有数据并从断点继续")
    return parser.parse_args()


def testset_fingerprint(testset: list[dict[str, Any]]) -> str:
    """计算测试集内容 SHA-256。"""
    payload = json.dumps(testset, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def git_commit_sha() -> str:
    """返回运行时 Git commit。"""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def load_partial_results(path: Path, run_id: str, fingerprint: str) -> list[dict[str, Any]]:
    """只加载同一 run_id 与测试集指纹的断点结果。"""
    if not path.is_file():
        return []
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        result = json.loads(line)
        if result.get("run_id") == run_id and result.get("testset_fingerprint") == fingerprint:
            unique.setdefault((result["model"], result["event_id"]), result)
    return list(unique.values())


def assert_full_is_unlocked() -> None:
    """要求最近一次隔离预验证完整且零错误，才允许正式运行。"""
    manifest_path = RUNS_ROOT / "validation" / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError("正式 benchmark 前必须先运行 --mode validation")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("status") != "completed"
        or manifest.get("actual_call_count") != VALIDATION_EVENTS * len(MODELS)
        or manifest.get("error_count") != 0
    ):
        raise RuntimeError("最近一次预验证未完整通过，禁止正式 benchmark")


def run_benchmark(mode: str, *, resume: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """执行一个隔离且可续跑的 validation/full run。"""
    if mode == "full" and not resume:
        assert_full_is_unlocked()
    paths = prepare_run_paths(mode, resume=resume)
    keys = load_api_keys()
    validate_credentials(keys)
    configs = get_model_configs(keys)
    full_testset = load_or_build_testset()
    testset = full_testset[:VALIDATION_EVENTS] if mode == "validation" else full_testset
    expected_events = VALIDATION_EVENTS if mode == "validation" else NUM_EVENTS
    if len(testset) != expected_events:
        raise RuntimeError(f"测试集数量错误：expected={expected_events}, actual={len(testset)}")
    fingerprint = testset_fingerprint(testset)

    if resume:
        manifest = json.loads(paths["manifest.json"].read_text(encoding="utf-8"))
        if manifest["testset_fingerprint"] != fingerprint:
            raise RuntimeError("无法续跑：测试集指纹与 manifest 不一致")
    else:
        started_at = datetime.now(timezone.utc)
        manifest = {
            "run_id": f"{mode}-{started_at.strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}",
            "mode": mode,
            "status": "running",
            "started_at": started_at.isoformat(),
            "completed_at": None,
            "git_commit": git_commit_sha(),
            "models": list(MODELS),
            "provider": "dashscope",
            "endpoint_host": urlparse(str(keys["dashscope"]["url"])).netloc,
            "enable_thinking": False,
            "event_count": len(testset),
            "expected_call_count": len(testset) * len(MODELS),
            "testset_fingerprint": fingerprint,
            "schema_retries": 2,
            "chunking_policy": {"target_chars": 12000, "overlap_turns": 2, "max_split_depth": 3},
        }
        write_json_atomic(paths["manifest.json"], manifest)
        paths["testset.jsonl"].write_text(
            "\n".join(json.dumps(event, ensure_ascii=False) for event in testset) + "\n",
            encoding="utf-8",
        )

    results = load_partial_results(paths["results.partial.jsonl"], manifest["run_id"], fingerprint)
    completed = {(result["model"], result["event_id"]) for result in results}
    print(f"run_id={manifest['run_id']} completed={len(completed)}/{manifest['expected_call_count']}")
    for config in configs:
        print(f"\n  ── {config['model']} ──")
        extractor = make_extractor(config)
        for index, event in enumerate(testset, start=1):
            result_key = (config["model"], event["id"])
            if result_key in completed:
                continue
            result = {
                "run_id": manifest["run_id"],
                "testset_fingerprint": fingerprint,
                "model": config["model"],
                "provider": config["provider"],
                "enable_thinking": config["enable_thinking"],
                "event_id": event["id"],
                "category": event["category"],
                "actor": event["actor_type"],
                **run_single_extraction(
                    extractor,
                    event["content"],
                    {
                        "session_id": event["session_id"],
                        "actor": event["actor_type"],
                        "actor_type": event["actor_type"],
                        "source_kind": event["category"],
                    },
                ),
            }
            append_jsonl(paths["results.partial.jsonl"], result)
            results.append(result)
            completed.add(result_key)
            if index % 10 == 0 or index == len(testset):
                model_results = [item for item in results if item["model"] == config["model"]]
                errors = sum(item["extraction_error"] is not None for item in model_results)
                print(f"    [{index}/{len(testset)}] ok={len(model_results) - errors} err={errors}")

    expected_keys = {(model, event["id"]) for model in MODELS for event in testset}
    actual_keys = {(result["model"], result["event_id"]) for result in results}
    if actual_keys != expected_keys or len(results) != len(expected_keys):
        raise RuntimeError(
            f"结果完整性失败：expected={len(expected_keys)}, actual={len(results)}, "
            f"missing={len(expected_keys - actual_keys)}, extra={len(actual_keys - expected_keys)}"
        )
    event_order = {event["id"]: index for index, event in enumerate(testset)}
    results.sort(key=lambda result: (MODELS.index(result["model"]), event_order[result["event_id"]]))
    paths["results.jsonl"].write_text(
        "\n".join(json.dumps(result, ensure_ascii=False) for result in results) + "\n",
        encoding="utf-8",
    )
    manifest["status"] = "completed"
    manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
    manifest["actual_call_count"] = len(results)
    manifest["error_count"] = sum(result["extraction_error"] is not None for result in results)
    write_json_atomic(paths["manifest.json"], manifest)
    return results, manifest


def assert_validation_passed(results: list[dict[str, Any]], manifest: dict[str, Any]) -> None:
    """确认 15 次预验证全部连通、无错误且延迟合理。"""
    if len(results) != VALIDATION_EVENTS * len(MODELS) or manifest["actual_call_count"] != 15:
        raise RuntimeError("预验证结果不完整")
    errors = [result for result in results if result["extraction_error"]]
    if errors:
        raise RuntimeError(f"预验证存在 {len(errors)} 个错误，禁止进入正式 benchmark")
    if any(result["latency_ms"] > 120_000 for result in results):
        raise RuntimeError("预验证存在超过 120 秒的调用，禁止进入正式 benchmark")


def main() -> None:
    """执行指定模式；预验证失败时保持结果并返回非零状态。"""
    args = parse_args()
    with exclusive_run_lock():
        results, manifest = run_benchmark(args.mode, resume=args.resume)
        if args.mode == "validation":
            assert_validation_passed(results, manifest)
        print_summary(results)
        print(f"\n结果目录: {RUNS_ROOT / args.mode}")


if __name__ == "__main__":
    main()
