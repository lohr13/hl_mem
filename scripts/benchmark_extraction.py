#!/usr/bin/env python
"""5模型提取质量横评：glm-5.2 / glm-5 / glm-4.7 / qwen3.7-plus / qwen3.6-plus。

用法：
  cd REDACTED_PATH
  .venv/Scripts/python.exe scripts/benchmark_extraction.py

输出：
  scripts/extraction_testset.jsonl      — 50条测试事件
  scripts/extraction_benchmark_results.jsonl — 逐条结果
  stdout                                — 汇总对比表
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
import traceback
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from hl_mem.ingest.llm_extractor import LLMExtractor
from hl_mem.ingest.chunking import ChunkingPolicy
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.ingest.repair import repair_extraction_json
from hl_mem.ingest.schemas import ExtractionResponseSchema
from hl_mem.llm.client import LLMClient
from hl_mem.llm.providers import DashScopeProvider, ZhipuProvider
from hl_mem.llm.types import LLMCapabilities, LLMRequest, LLMMessage
from hl_mem.observability.audit import current_audit
from hl_mem.storage._shared import decode_json

# ─── 配置 ───────────────────────────────────────────────────────────────

DB_PATH = PROJECT_ROOT / "var" / "hl_mem.db"
TESTSET_PATH = PROJECT_ROOT / "scripts" / "extraction_testset.jsonl"
RESULTS_PATH = PROJECT_ROOT / "scripts" / "extraction_benchmark_results.jsonl"
PARTIAL_RESULTS_PATH = PROJECT_ROOT / "scripts" / "extraction_benchmark_results.partial.jsonl"
HERMES_CONFIG_PATH = Path("C:/Users/Administrator/AppData/Local/hermes/config.yaml")
NUM_EVENTS = 20


def _read_hermes_dashscope_config(path: Path) -> dict[str, str | None]:
    """从 Hermes YAML 配置中读取 DashScope 凭据。"""
    result: dict[str, str | None] = {"key": None, "url": None}
    if not path.is_file():
        return result

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
            continue
        if in_providers and indent == 2:
            in_dashscope = stripped == "dashscope:"
            continue
        if in_dashscope and indent == 4 and ":" in stripped:
            name, value = stripped.split(":", 1)
            value = value.strip().strip("'\"")
            if name == "api_key":
                result["key"] = value
            elif name == "base_url":
                result["url"] = value
    return result


def load_api_keys() -> dict[str, dict[str, str | None]]:
    """从 .env 读取百炼 API 凭据。"""
    env_path = PROJECT_ROOT / ".env"
    zhipu_key = zhipu_url = None
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("LLM_API_KEY="):
                zhipu_key = line.split("=", 1)[1].strip()
            elif line.startswith("LLM_BASE_URL="):
                zhipu_url = line.split("=", 1)[1].strip()

    return {
        "zhipu": {"key": zhipu_key, "url": zhipu_url},
        "dashscope": _read_hermes_dashscope_config(HERMES_CONFIG_PATH),
    }


# ─── 1. 构建测试集 ─────────────────────────────────────────────────────

def build_testset() -> list[dict]:
    """从数据库采样 diverse events。"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    events = []

    # 按类别采样
    queries = [
        ("user_pref", "actor_type='user'", 10),
        ("project_config", "content_json LIKE '%hl_mem%' OR content_json LIKE '%config%'", 10),
        ("tool_workflow", "actor_type='tool' OR content_json LIKE '%Codex%' OR content_json LIKE '%pytest%'", 10),
        ("status_report", "content_json LIKE '%healthz%' OR content_json LIKE '%status%'", 5),
        ("chat_confirm", "actor_type='assistant' AND LENGTH(content_json) < 300", 5),
        ("long_content", "LENGTH(content_json) > 2000", 10),
    ]

    seen_ids = set()
    for category, where_clause, limit in queries:
        rows = conn.execute(
            f"SELECT * FROM events WHERE {where_clause} "
            f"AND id NOT IN ({','.join('?' * len(seen_ids)) or 'NULL'}) "
            "ORDER BY RANDOM() LIMIT ?",
            (*seen_ids, limit) if seen_ids else (limit,),
        ).fetchall() if seen_ids else conn.execute(
            f"SELECT * FROM events WHERE {where_clause} ORDER BY RANDOM() LIMIT ?",
            (limit,),
        ).fetchall()

        for row in rows:
            if row["id"] not in seen_ids:
                content = decode_json(row["content_json"])
                text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                events.append({
                    "id": row["id"],
                    "category": category,
                    "actor_type": row["actor_type"],
                    "session_id": row["session_id"] if row["session_id"] else "",
                    "content": text[:2000],  # 截取避免太长
                    "content_length": len(text),
                })
                seen_ids.add(row["id"])

    remaining = NUM_EVENTS - len(events)
    if remaining > 0:
        placeholders = ",".join("?" * len(seen_ids))
        rows = conn.execute(
            f"SELECT * FROM events WHERE id NOT IN ({placeholders}) ORDER BY RANDOM() LIMIT ?",
            (*seen_ids, remaining),
        ).fetchall()
        for row in rows:
            content = decode_json(row["content_json"])
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            events.append({
                "id": row["id"],
                "category": "random_fill",
                "actor_type": row["actor_type"],
                "session_id": row["session_id"] if row["session_id"] else "",
                "content": text[:2000],
                "content_length": len(text),
            })

    conn.close()
    return events[:NUM_EVENTS]


def load_or_build_testset() -> list[dict]:
    """复用已落盘测试集，避免中断重跑时改变样本。"""
    if TESTSET_PATH.is_file():
        testset = [
            json.loads(line)
            for line in TESTSET_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if len(testset) == NUM_EVENTS:
            return testset

    testset = build_testset()
    TESTSET_PATH.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in testset) + "\n",
        encoding="utf-8",
    )
    return testset


def testset_fingerprint(testset: list[dict]) -> str:
    """返回测试集内容指纹，隔离不同随机样本的断点文件。"""
    payload = json.dumps(testset, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_partial_results(fingerprint: str) -> list[dict]:
    """读取与当前测试集匹配的断点结果。"""
    if not PARTIAL_RESULTS_PATH.is_file():
        return []
    results_by_key: dict[tuple[str, str], dict] = {}
    for line in PARTIAL_RESULTS_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        result = json.loads(line)
        if result.get("testset_fingerprint") == fingerprint:
            key = (result["model"], result["event_id"])
            results_by_key.setdefault(key, result)
    return list(results_by_key.values())


# ─── 2. 模型配置 ───────────────────────────────────────────────────────

def get_model_configs(keys: dict[str, dict[str, str | None]]) -> list[dict[str, str]]:
    """返回统一使用百炼端点的五模型 benchmark 配置。"""
    model_specs = (
        ("glm-5.2", "zhipu"),
        ("glm-5", "zhipu"),
        ("glm-4.7", "zhipu"),
        ("qwen3.7-plus", "dashscope"),
        ("qwen3.6-plus", "dashscope"),
    )
    return [
        {
            "name": model,
            "provider": provider,
            "model": model,
            "api_key": keys[provider]["key"],
            "base_url": keys[provider]["url"],
        }
        for model, provider in model_specs
        if keys[provider]["key"] and keys[provider]["url"]
    ]


# ─── 3. 单模型提取 + 指标采集 ──────────────────────────────────────────

def make_extractor(config: dict) -> LLMExtractor | None:
    """构造一个指定模型的 extractor。"""
    provider = DashScopeProvider(enable_thinking=False) if config["provider"] == "dashscope" else ZhipuProvider()

    import httpx
    client = LLMClient(
        api_key=config["api_key"],
        base_url=config["base_url"],
        model=config["model"],
        provider=provider,
        timeout=httpx.Timeout(120.0, connect=15.0),
        max_attempts=2,
        operation="benchmark",
    )

    from hl_mem.llm.types import StructuredOutputMode
    extractor = LLMExtractor(
        llm_client=client,
        schema_retries=2,
        structured_mode=StructuredOutputMode.JSON_OBJECT,
        chunking_policy=ChunkingPolicy(target_chars=12000, overlap_turns=2, max_split_depth=3),
    )
    return extractor


def run_single_extraction(extractor: LLMExtractor, content: str, context: dict) -> dict:
    """运行一次提取，返回指标。"""
    t0 = time.time()
    metrics = {
        "schema_retry_count": 0,
        "repair_count": 0,
        "llm_call_count": 0,
        "should_memorize": False,
        "claims_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "extraction_error": None,
        "claims_data": [],
    }

    try:
        # 重置计数器
        extractor._schema_retry_count = 0
        extractor._repair_count = 0
        extractor._llm_call_count = 0
        extractor._memorize_decisions = []

        claims = extractor.extract(content, context)
        elapsed = time.time() - t0

        metrics["schema_retry_count"] = getattr(extractor, "_schema_retry_count", 0)
        metrics["repair_count"] = getattr(extractor, "_repair_count", 0)
        metrics["llm_call_count"] = getattr(extractor, "_llm_call_count", 0)
        metrics["should_memorize"] = len(claims) > 0
        metrics["claims_count"] = len(claims)
        metrics["input_tokens"] = extractor.last_input_tokens
        metrics["output_tokens"] = extractor.last_output_tokens
        metrics["total_tokens"] = extractor.last_usage_tokens
        metrics["latency_ms"] = round(elapsed * 1000)

        for c in claims:
            metrics["claims_data"].append({
                "subject": c.subject,
                "predicate": c.predicate,
                "scope": c.scope,
                "importance": c.importance,
            })

    except Exception as e:
        elapsed = time.time() - t0
        metrics["extraction_error"] = f"{type(e).__name__}: {str(e)[:200]}"
        metrics["latency_ms"] = round(elapsed * 1000)

    return metrics


# ─── 4. 主流程 ─────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("  hl_mem 提取质量横评 — 5 模型对比")
    print("=" * 70)

    # 加载凭据
    keys = load_api_keys()
    model_configs = get_model_configs(keys)
    print(f"\n凭据: dashscope={'✓' if keys['dashscope']['key'] else '✗'}")
    print(f"模型: {[c['name'] for c in model_configs]}")

    # 构建测试集
    print("\n[1/3] 构建测试集...")
    testset = load_or_build_testset()
    print(f"  采样 {len(testset)} 条 events")
    cat_counts = Counter(e["category"] for e in testset)
    for cat, cnt in cat_counts.most_common():
        print(f"    {cat}: {cnt}")

    # 横评
    print(f"\n[2/3] 运行 {len(testset)} events × {len(model_configs)} models = {len(testset) * len(model_configs)} calls...")
    fingerprint = testset_fingerprint(testset)
    all_results = load_partial_results(fingerprint)
    completed = {(result["model"], result["event_id"]) for result in all_results}
    if all_results:
        print(f"  断点续跑: 已完成 {len(all_results)} calls")

    for cfg in model_configs:
        print(f"\n  ── {cfg['name']} ──")
        extractor = make_extractor(cfg)
        if extractor is None:
            print(f"    SKIP: 无法创建 extractor")
            continue

        for i, event in enumerate(testset):
            result_key = (cfg["name"], event["id"])
            if result_key in completed:
                continue
            result = {
                "model": cfg["name"],
                "event_id": event["id"],
                "category": event["category"],
                "actor": event["actor_type"],
                **run_single_extraction(extractor, event["content"], {
                    "session_id": event["session_id"],
                    "actor": event["actor_type"],
                }),
                "testset_fingerprint": fingerprint,
            }
            all_results.append(result)
            with PARTIAL_RESULTS_PATH.open("a", encoding="utf-8") as partial_file:
                partial_file.write(json.dumps(result, ensure_ascii=False) + "\n")
                partial_file.flush()

            if (i + 1) % 10 == 0:
                ok = sum(1 for r in all_results if r["model"] == cfg["name"] and not r["extraction_error"])
                print(f"    [{i+1}/{len(testset)}] ok={ok} err={i+1-ok}")

    # 输出原始结果
    RESULTS_PATH.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in all_results) + "\n",
        encoding="utf-8",
    )

    # 汇总
    print(f"\n[3/3] 汇总对比")
    print("=" * 70)
    print_summary(all_results)

    print(f"\n原始结果: {RESULTS_PATH}")
    print(f"测试集: {TESTSET_PATH}")


def print_summary(results: list[dict]):
    """打印对比表格。"""
    models = sorted(set(r["model"] for r in results))

    # ── 按模型汇总
    stats = {}
    for model in models:
        mr = [r for r in results if r["model"] == model and r["extraction_error"] is None]
        err = [r for r in results if r["model"] == model and r["extraction_error"] is not None]
        total = len(mr) + len(err)

        all_claims = [c for r in mr for c in r.get("claims_data", [])]
        subjects = set(c["subject"] for c in all_claims)
        predicates = Counter(c["predicate"] for c in all_claims)

        stats[model] = {
            "total": total,
            "errors": len(err),
            "error_examples": [r["extraction_error"] for r in err[:3]],
            "should_memorize_rate": sum(1 for r in mr if r["should_memorize"]) / max(len(mr), 1),
            "avg_claims": sum(r["claims_count"] for r in mr) / max(len(mr), 1),
            "avg_retries": sum(r["schema_retry_count"] for r in mr) / max(len(mr), 1),
            "avg_repairs": sum(r["repair_count"] for r in mr) / max(len(mr), 1),
            "avg_tokens": sum(r["total_tokens"] for r in mr) / max(len(mr), 1),
            "avg_latency": sum(r["latency_ms"] for r in mr) / max(len(mr), 1),
            "subject_diversity": len(subjects),
            "top_predicates": predicates.most_common(3),
            "claim_count": len(all_claims),
        }

    # ── 打印表格
    print(f"\n{'指标':<28}", end="")
    for m in models:
        print(f" | {m:>14}", end="")
    print()
    print("-" * (28 + (18 * len(models))))

    rows = [
        ("总调用数", "total", "d"),
        ("错误数", "errors", "d"),
        ("should_memorize 率", "should_memorize_rate", ".1%"),
        ("avg claims/event", "avg_claims", ".1f"),
        ("avg schema retries", "avg_retries", ".2f"),
        ("avg repair count", "avg_repairs", ".2f"),
        ("avg total tokens", "avg_tokens", ".0f"),
        ("avg latency (ms)", "avg_latency", ".0f"),
        ("subject diversity", "subject_diversity", "d"),
        ("总 claims 数", "claim_count", "d"),
    ]

    for label, key, fmt in rows:
        print(f"{label:<28}", end="")
        for m in models:
            val = stats[m][key]
            if fmt == "d":
                print(f" | {int(val):>14}", end="")
            elif fmt == ".1%":
                print(f" | {val:>13.1%}", end="")
            else:
                print(f" | {val:>14{fmt}}", end="")
        print()

    # ── Predicate 分布
    print(f"\nTop-3 predicate:")
    for m in models:
        preds = stats[m]["top_predicates"]
        pred_str = ", ".join(f"{p}({c})" for p, c in preds)
        print(f"  {m}: {pred_str}")

    # ── 错误示例
    has_errors = any(stats[m]["errors"] > 0 for m in models)
    if has_errors:
        print(f"\n错误示例:")
        for m in models:
            for err in stats[m]["error_examples"]:
                print(f"  [{m}] {err}")

    # ── 按类别分解
    print(f"\n按事件类别 — claims_count:")
    categories = sorted(set(r["category"] for r in results))
    print(f"{'category':<20}", end="")
    for m in models:
        print(f" | {m:>14}", end="")
    print()
    for cat in categories:
        print(f"{cat:<20}", end="")
        for m in models:
            cr = [r for r in results if r["model"] == m and r["category"] == cat and not r["extraction_error"]]
            avg_c = sum(r["claims_count"] for r in cr) / max(len(cr), 1) if cr else 0
            print(f" | {avg_c:>14.1f}", end="")
        print()


if __name__ == "__main__":
    main()
