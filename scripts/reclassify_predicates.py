"""使用 LLM 重新分类仍标记为“事实”的活跃 claim。"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

import httpx

VALID_PREDICATES = ("偏好", "使用", "状态", "身份", "配置", "计划", "事实")
SYSTEM_PROMPT = (
    "你是数据分类器。给定一条记忆的 subject、value、scope，判断它应该归入哪个 predicate。"
    "只返回 predicate 名称（偏好/使用/状态/身份/配置/计划/事实），不要解释。"
)
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_ATTEMPTS = 3


def load_env(path: Path) -> None:
    """从 dotenv 文件加载尚未设置的环境变量。"""
    if not path.is_file():
        raise FileNotFoundError(f"找不到环境配置文件: {path}")
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def require_env(name: str) -> str:
    """读取必需配置，缺失时给出明确错误。"""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少必需环境变量: {name}")
    return value


def decode_value(value_json: str | None) -> Any:
    """解析 value_json，字典值优先使用 text 字段。"""
    try:
        value = json.loads(value_json or "null")
    except json.JSONDecodeError:
        return value_json or ""
    return value.get("text", value) if isinstance(value, dict) else value


def classify_claim(
    client: httpx.Client,
    *,
    endpoint: str,
    model: str,
    subject: str,
    value: Any,
    scope: str,
) -> str:
    """逐条调用兼容 OpenAI 的接口，失败时有限重试。"""
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {"subject": subject, "value": value, "scope": scope}, ensure_ascii=False, default=str
                ),
            },
        ],
        "enable_thinking": False,
    }
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.post(endpoint, json=body)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("LLM 响应 content 不是字符串")
            candidate = content.strip().strip("`\"'，。 ")
            return candidate if candidate in VALID_PREDICATES else "事实"
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            if attempt == MAX_ATTEMPTS:
                raise
            time.sleep(float(attempt))
    raise RuntimeError("unreachable")


def print_summary(total: int, counts: Counter[str], errors: int) -> None:
    """打印分类汇总。"""
    print("\n分类汇总")
    print(f"原“事实”总数: {total}")
    for predicate in VALID_PREDICATES[:-1]:
        print(f"重新分类到“{predicate}”: {counts[predicate]}")
    print(f"仍为“事实”: {counts['事实']}")
    print(f"调用失败并跳过: {errors}")


def run(database: Path, env_file: Path, *, dry_run: bool) -> int:
    """执行迁移；dry-run 模式只打印而不写入。"""
    load_env(env_file)
    model = require_env("LLM_MODEL")
    base_url = require_env("LLM_BASE_URL").rstrip("/")
    api_key = require_env("LLM_API_KEY")
    connection = sqlite3.connect(database, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    try:
        claims = connection.execute(
            "SELECT id,subject_entity_id,value_json,scope FROM claims "
            "WHERE predicate=? AND expires_at IS NULL AND superseded_by_id IS NULL ORDER BY id",
            ("事实",),
        ).fetchall()
        counts: Counter[str] = Counter()
        errors = 0
        print(f"[{'DRY-RUN' if dry_run else 'APPLY'}] 待处理: {len(claims)}，模型: {model}")
        with httpx.Client(
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        ) as client:
            for index, claim in enumerate(claims, start=1):
                try:
                    predicate = classify_claim(
                        client,
                        endpoint=f"{base_url}/chat/completions",
                        model=model,
                        subject=str(claim["subject_entity_id"] or ""),
                        value=decode_value(claim["value_json"]),
                        scope=str(claim["scope"] or ""),
                    )
                    counts[predicate] += 1
                    if not dry_run and predicate != "事实":
                        connection.execute(
                            "UPDATE claims SET predicate=? WHERE id=? AND predicate=? "
                            "AND expires_at IS NULL AND superseded_by_id IS NULL",
                            (predicate, claim["id"], "事实"),
                        )
                    print(f"[{index}/{len(claims)}] {claim['id']} -> {predicate}", flush=True)
                except Exception as error:
                    errors += 1
                    print(f"warning: [{index}/{len(claims)}] {claim['id']} 跳过: {error}", file=sys.stderr, flush=True)
        connection.rollback() if dry_run else connection.commit()
        print_summary(len(claims), counts, errors)
        return 0
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("var/hl_mem.db"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """脚本入口。"""
    args = build_parser().parse_args(argv)
    return run(args.database, args.env_file, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
