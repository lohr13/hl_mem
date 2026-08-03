"""面向日常记忆操作的 HTTP-first CLI 命令。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

DEFAULT_SERVER_URL = "http://127.0.0.1:8200"
DAILY_HTTP_COMMANDS = frozenset({"remember", "recall", "list", "forget"})

OFFLINE_CONFIG = """# HL-Mem offline configuration: no API keys or network models required.
# Fake embedding is deterministic storage compatibility data, not semantic search.

[database]
path = "var/hl_mem.db"

[extraction]
mode = "fake"

[embedding]
mode = "fake"
dim = 2048

[reranker]
mode = "off"

[image_describer]
mode = "off"

[recall]
dense_enabled = false
query_expansion_mode = "off"
tag_channel_enabled = false
relevance_gate_mode = "off"

[relation]
expansion_mode = "off"
discovery_mode = "off"

[dedup]
enabled = false
"""

ONLINE_CONFIG = """# HL-Mem model-backed configuration.
# Put enabled components' API keys in .env; never commit real keys.

[database]
path = "var/hl_mem.db"

[llm]
provider = "dashscope"
base_url = "https://coding.dashscope.aliyuncs.com/v1"
model = "qwen3.7-plus"
structured_mode = "json_object"

[extraction]
mode = "llm"

[embedding]
mode = "real"
model = "text-embedding-v4"
dim = 2048

[reranker]
mode = "on"
provider = "dashscope"
model = "gte-rerank-v2"

[image_describer]
mode = "off"

[recall]
dense_enabled = true
query_expansion_mode = "auto"
query_expansion_model = "glm-4.7"

[relation]
expansion_mode = "off"
discovery_mode = "off"
"""


def add_daily_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """注册不改变既有运维命令的日常子命令。"""
    init = commands.add_parser("init", help="生成 hl_mem.toml 配置")
    init.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    init.add_argument("--offline", action="store_true", help="生成无需 API key 的 FTS-only 配置")
    init.add_argument("--force", action="store_true", help="覆盖已有配置文件")

    server = commands.add_parser("server", help="启动本地 API 与后台 Worker")
    server.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    server.add_argument("--env-file", type=Path, default=argparse.SUPPRESS)
    server.add_argument("--db", type=Path, default=argparse.SUPPRESS)
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8200)

    remember = commands.add_parser("remember", help="写入一条显式记忆")
    remember.add_argument("text")
    _add_url(remember)

    recall = commands.add_parser("recall", help="按文本召回记忆")
    recall.add_argument("query")
    recall.add_argument("--limit", type=int, choices=range(1, 101))
    _add_url(recall)

    list_command = commands.add_parser("list", help="分页列出记忆")
    list_command.add_argument("--limit", type=int, choices=range(1, 101), default=20)
    list_command.add_argument("--offset", type=int, default=0)
    list_command.add_argument(
        "--status",
        choices=("active", "candidate", "disputed", "superseded", "expired", "archived", "retracted"),
        default="active",
    )
    list_command.add_argument("--namespace", default="default")
    _add_url(list_command)

    forget = commands.add_parser("forget", help="按 Claim ID 撤回记忆")
    forget.add_argument("memory_id")
    _add_url(forget)


def _add_url(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--url",
        default=os.environ.get("HL_MEM_URL", DEFAULT_SERVER_URL),
        help=f"HL-Mem 服务地址（默认 {DEFAULT_SERVER_URL}）",
    )


def initialize_config(path: Path, *, offline: bool, force: bool, parser: argparse.ArgumentParser) -> None:
    """生成配置；已有文件必须显式 --force。"""
    if path.exists() and not force:
        parser.error(f"配置文件已存在：{path}；如需覆盖请加 --force")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(OFFLINE_CONFIG if offline else ONLINE_CONFIG, encoding="utf-8", newline="\n")
    print(f"已生成配置：{path}")
    if offline:
        print("当前为无 AK 的 FTS-only 关键词召回；fake embedding 不提供语义检索。")
        print("切换真实模型：填写 .env 密钥，并将 extraction/embedding/reranker 等 mode 改为真实模式。")
    else:
        print("请按启用的组件在 .env 中填写独立 API key，然后运行 `hl-mem server`。")


def handle_daily_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> bool:
    """执行 init 或 HTTP 日常命令；已处理时返回 True。"""
    if args.command == "init":
        initialize_config(
            args.config or Path("hl_mem.toml"),
            offline=args.offline,
            force=args.force,
            parser=parser,
        )
        return True
    if args.command not in DAILY_HTTP_COMMANDS:
        return False
    if args.command == "remember":
        result = _request_json(args.url, "POST", "/v1/memories", json_body={"text": args.text})
        state = "已提交记忆" if result.get("created", True) else "记忆已存在"
        print(f"{state}（事件 ID：{result.get('id', '-')}）。处理完成后可用 recall/list 查看 Claim ID。")
        return True
    if args.command == "recall":
        payload: dict[str, Any] = {"query": args.query}
        if args.limit is not None:
            payload["limit"] = args.limit
        result = _request_json(args.url, "POST", "/v1/recall", json_body=payload)
        _print_recall(result)
        return True
    if args.command == "list":
        result = _request_json(
            args.url,
            "GET",
            "/v1/memories",
            params={
                "limit": args.limit,
                "offset": args.offset,
                "status": args.status,
                "namespace": args.namespace,
            },
        )
        _print_memory_page(result)
        return True
    result = _request_json(args.url, "DELETE", f"/v1/memories/{args.memory_id}")
    print(f"已撤回记忆 {result.get('id', args.memory_id)}。")
    return True


def _make_http_client(base_url: str) -> httpx.Client:
    """构造日常命令共用的短连接 HTTP 客户端。"""
    return httpx.Client(base_url=base_url.rstrip("/"), timeout=30.0)


def _request_json(
    base_url: str,
    method: str,
    path: str,
    *,
    json_body: Mapping[str, Any] | None = None,
    params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        with _make_http_client(base_url) as client:
            response = client.request(method, path, json=json_body, params=params)
            response.raise_for_status()
    except httpx.HTTPStatusError as error:
        detail = _error_detail(error.response)
        print(f"HL-Mem 请求失败（HTTP {error.response.status_code}）：{detail}", file=sys.stderr)
        raise SystemExit(1) from error
    except httpx.HTTPError as error:
        print(
            f"无法连接 HL-Mem 服务 {base_url}：{error}。请先运行 `hl-mem server`。",
            file=sys.stderr,
        )
        raise SystemExit(1) from error
    try:
        payload = response.json()
    except ValueError as error:
        print("HL-Mem 返回了无法解析的响应。", file=sys.stderr)
        raise SystemExit(1) from error
    if not isinstance(payload, dict):
        print("HL-Mem 返回格式不符合预期。", file=sys.stderr)
        raise SystemExit(1)
    return payload


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:500] or "unknown error"
    return str(payload.get("detail", payload)) if isinstance(payload, dict) else str(payload)


def _print_recall(payload: Mapping[str, Any]) -> None:
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        print("未找到匹配记忆。")
        return
    print(f"找到 {len(results)} 条记忆：")
    for index, raw_item in enumerate(results, 1):
        item = raw_item if isinstance(raw_item, Mapping) else {}
        try:
            score = float(item.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        print(f"\n[{index}] {item.get('text', '')}")
        print(f"    ID: {item.get('id', '-')}")
        print(f"    分数: {score:.4f}")
        evidence = item.get("evidence")
        if isinstance(evidence, list) and evidence:
            print("    证据:")
            for reference in evidence:
                print(f"      - {_format_evidence(reference)}")
        else:
            print("    证据: 无")


def _format_evidence(reference: Any) -> str:
    if not isinstance(reference, Mapping):
        return str(reference)
    source_type = reference.get("source_type")
    source_id = reference.get("source_id")
    if source_type or source_id:
        return "/".join(str(value) for value in (source_type, source_id) if value)
    return json.dumps(dict(reference), ensure_ascii=False, sort_keys=True)


def _print_memory_page(payload: Mapping[str, Any]) -> None:
    memories = payload.get("memories")
    total = int(payload.get("total", 0) or 0)
    offset = int(payload.get("offset", 0) or 0)
    if not isinstance(memories, list) or not memories:
        print(f"没有可列出的记忆（共 {total} 条）。")
        return
    print(f"记忆 {offset + 1}-{offset + len(memories)}（共 {total} 条）：")
    for raw_item in memories:
        item = raw_item if isinstance(raw_item, Mapping) else {}
        print(f"- [{item.get('id', '-')}] {item.get('text', '')}")
        print(f"  状态: {item.get('status', '-')}; 记录时间: {item.get('recorded_from', '-')}")
