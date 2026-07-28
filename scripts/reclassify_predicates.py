"""使用 LLM 重新分类历史 active“事实”claims。"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

DATABASE_PATH = Path("var/hl_mem.db")
ENV_PATH = Path(".env")
VALID_PREDICATES = ("偏好", "使用", "状态", "身份", "配置", "计划", "事实")
SYSTEM_PROMPT = (
    "你是数据分类器。给定一条记忆的 subject、value、scope，判断它应该归入哪个 predicate。"
    "只返回 predicate 名称，从 [偏好,使用,状态,身份,配置,计划,事实] 中选一个，不要解释。"
)


def load_env(path: Path) -> None:
    """从 dotenv 文件加载环境变量，保留调用方已设置的值。"""
    if not path.is_file():
        raise FileNotFoundError(f"环境变量文件不存在: {path}")
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def require_llm_config() -> tuple[str, str, str]:
    """读取并校验 LLM 调用配置。"""
    missing = [name for name in ("LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY") if not os.getenv(name)]
    if missing:
        raise RuntimeError(f".env 缺少 LLM 配置: {', '.join(missing)}")
    return os.environ["LLM_MODEL"], os.environ["LLM_BASE_URL"].rstrip("/"), os.environ["LLM_API_KEY"]


def decode_value(value_json: str | None) -> Any:
    """解析 claim 值，字典值优先提取 text 字段。"""
    try:
        value = json.loads(value_json or "null")
    except json.JSONDecodeError:
        return value_json or ""
    if isinstance(value, dict) and "text" in value:
        return value["text"]
    return value


def classify_claim(
    client: httpx.Client,
    *,
    url: str,
    model: str,
    subject: str,
    value: Any,
    scope: str,
) -> str:
    """调用兼容 OpenAI 的接口，并严格校验 predicate 返回值。"""
    user_content = json.dumps(
        {"subject": subject, "value": value, "scope": scope},
        ensure_ascii=False,
        default=str,
    )
    response = client.post(
        f"{url}/chat/completions",
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "enable_thinking": False,
        },
    )
    response.raise_for_status()
    predicate = str(response.json()["choices"][0]["message"]["content"]).strip()
    if predicate not in VALID_PREDICATES:
        raise ValueError(f"LLM 返回非法 predicate: {predicate!r}")
    return predicate


def print_distribution(connection: sqlite3.Connection) -> None:
    """打印所有 active claims 的 predicate 分布。"""
    rows = connection.execute(
        "SELECT predicate, count(*) FROM claims "
        "WHERE expires_at IS NULL AND superseded_by_id IS NULL "
        "GROUP BY predicate ORDER BY count(*) DESC, predicate"
    ).fetchall()
    print("新的 active predicate 分布:")
    for predicate, count in rows:
        print(f"  {predicate or '<NULL>'}: {count}")


def main() -> int:
    """逐条重新分类 active“事实”claims，并提交数据库更新。"""
    load_env(ENV_PATH)
    model, base_url, api_key = require_llm_config()
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    reclassified: Counter[str] = Counter()
    remained_facts = 0
    errors = 0
    try:
        claims = connection.execute(
            "SELECT id, subject_entity_id, value_json, scope FROM claims "
            "WHERE predicate = ? AND expires_at IS NULL AND superseded_by_id IS NULL ORDER BY id",
            ("事实",),
        ).fetchall()
        print(f"待分类 active 事实 claims: {len(claims)}")
        with httpx.Client(
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=30.0,
        ) as client:
            for index, claim in enumerate(claims, start=1):
                try:
                    predicate = classify_claim(
                        client,
                        url=base_url,
                        model=model,
                        subject=str(claim["subject_entity_id"] or ""),
                        value=decode_value(claim["value_json"]),
                        scope=str(claim["scope"] or ""),
                    )
                    if predicate == "事实":
                        remained_facts += 1
                    else:
                        cursor = connection.execute(
                            "UPDATE claims SET predicate = ? "
                            "WHERE id = ? AND predicate = ? AND expires_at IS NULL AND superseded_by_id IS NULL",
                            (predicate, claim["id"], "事实"),
                        )
                        if cursor.rowcount != 1:
                            raise RuntimeError(f"claim 更新条件不再成立: {claim['id']}")
                        reclassified[predicate] += 1
                    print(f"[{index}/{len(claims)}] {claim['id']} -> {predicate}")
                except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, RuntimeError) as error:
                    errors += 1
                    remained_facts += 1
                    print(f"[{index}/{len(claims)}] {claim['id']} 跳过: {error}")
        connection.commit()
        print("重分类汇总:")
        for predicate in VALID_PREDICATES[:-1]:
            print(f"  -> {predicate}: {reclassified[predicate]}")
        print(f"  仍为事实: {remained_facts}")
        print(f"  调用/返回错误: {errors}")
        print_distribution(connection)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
