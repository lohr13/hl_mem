"""面向日常记忆操作的 HTTP-first CLI 命令。"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import tempfile
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import httpx
import tomli_w

from hl_mem.config.loader import load_settings_data
from hl_mem.config.models import EmbeddingApiMode, LLMProvider, Settings
from hl_mem.config.secrets import merge_secret_file, redact_secret_text
from hl_mem.doctor import CheckStatus, probe_model_components
from hl_mem.errors import ConfigurationError

DEFAULT_SERVER_URL = "http://127.0.0.1:8200"
DAILY_HTTP_COMMANDS = frozenset({"remember", "recall", "list", "forget", "correct"})


def add_daily_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """注册不改变既有运维命令的日常子命令。"""
    init = commands.add_parser("init", help="生成 hl_mem.toml 配置")
    init.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    init.add_argument("--env-file", type=Path, default=argparse.SUPPRESS)
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

    correct = commands.add_parser("correct", help="按 Claim ID 纠正记忆")
    correct.add_argument("memory_id")
    correct.add_argument("--text", required=True, help="纠正后的记忆文本")
    _add_url(correct)


def _add_url(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--url",
        default=os.environ.get("HL_MEM_URL", DEFAULT_SERVER_URL),
        help=f"HL-Mem 服务地址（默认 {DEFAULT_SERVER_URL}）",
    )


def _required_prompt(label: str) -> str:
    value = input(f"{label}: ").strip()
    if not value:
        raise ConfigurationError(f"{label} is required")
    return value


def _secret_prompt(label: str) -> str:
    value = getpass.getpass(f"{label}: ").strip()
    if not value:
        raise ConfigurationError(f"{label} is required")
    return value


def _write_config_atomic(path: Path, document: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(document.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _restore_secret_file(path: Path, existed: bool, content: bytes | None) -> None:
    if not existed:
        path.unlink(missing_ok=True)
        return
    assert content is not None
    path.write_bytes(content)


def _collect_init_settings() -> tuple[Settings, dict[str, str]]:
    provider_value = _required_prompt("LLM provider (dashscope/zhipu/openai_compatible)")
    if provider_value not in {"dashscope", "zhipu", "openai_compatible"}:
        raise ConfigurationError("LLM provider must be dashscope, zhipu, or openai_compatible")
    llm_base_url = _required_prompt("LLM base URL")
    llm_model = _required_prompt("LLM model")
    llm_key = _secret_prompt("LLM API key")
    embedding_base_url = _required_prompt("Embedding base URL")
    embedding_model = _required_prompt("Embedding model")
    raw_dimension = _required_prompt("Embedding dimension")
    try:
        embedding_dim = int(raw_dimension)
    except ValueError as error:
        raise ConfigurationError("Embedding dimension must be an integer") from error
    embedding_api_value = _required_prompt("Embedding API mode (compatible/native)")
    if embedding_api_value not in {"compatible", "native"}:
        raise ConfigurationError("Embedding API mode must be compatible or native")
    embedding_key = _secret_prompt("Embedding API key")
    reranker_choice = _required_prompt("Enable the built-in DashScope reranker? (y/n)").lower()
    if reranker_choice not in {"y", "yes", "n", "no"}:
        raise ConfigurationError("Reranker choice must be y or n")

    values: dict[str, Any] = {
        "llm_provider": cast(LLMProvider, provider_value),
        "llm_base_url": llm_base_url,
        "llm_model": llm_model,
        "llm_api_key": llm_key,
        "extractor_mode": "llm",
        "embedder_mode": "real",
        "embedding_base_url": embedding_base_url,
        "embedding_model": embedding_model,
        "embedding_dim": embedding_dim,
        "embedding_api_mode": cast(EmbeddingApiMode, embedding_api_value),
        "embedding_api_key": embedding_key,
        "query_expansion_mode": "off",
        "resurrection_mode": "off",
        "relation_discovery_mode": "off",
        "image_describer_mode": "off",
    }
    secrets = {"LLM_API_KEY": llm_key, "EMBEDDING_API_KEY": embedding_key}
    if reranker_choice in {"y", "yes"}:
        values.update(
            reranker_mode="real",
            reranker_provider="dashscope",
            reranker_base_url=_required_prompt("Reranker base URL"),
            reranker_model=_required_prompt("Reranker model"),
            reranker_api_key=_secret_prompt("Reranker API key"),
        )
        secrets["RERANKER_API_KEY"] = str(values["reranker_api_key"])
    settings = Settings(**values)
    settings.validate_runtime()
    return settings, secrets


def _render_init_config(settings: Settings) -> str:
    document: dict[str, Any] = {
        "schema_version": 1,
        "database": {"path": "var/hl_mem.db"},
        "llm": {
            "provider": settings.llm_provider,
            "base_url": settings.llm_base_url,
            "model": settings.llm_model,
            "structured_mode": settings.llm_structured_mode,
        },
        "extraction": {"mode": "llm"},
        "embedding": {
            "mode": "real",
            "base_url": settings.embedding_base_url,
            "model": settings.embedding_model,
            "dim": settings.embedding_dim,
            "api_mode": settings.embedding_api_mode,
        },
        "reranker": {
            "mode": settings.reranker_mode,
            "provider": settings.reranker_provider,
            "base_url": settings.reranker_base_url,
            "model": settings.reranker_model,
        },
        "image_describer": {"mode": "off"},
        "recall": {
            "query_expansion_mode": "off",
            "resurrection_mode": "off",
        },
        "relation": {"expansion_mode": "off", "discovery_mode": "off"},
        "plugins": {"enabled": []},
    }
    return tomli_w.dumps(document)


def initialize_config(
    path: Path,
    *,
    env_path: Path,
    force: bool,
    parser: argparse.ArgumentParser,
) -> None:
    """Collect, verify, then atomically commit a production configuration."""
    if path.exists() and not force:
        parser.error(f"配置文件已存在：{path}；如需覆盖请加 --force")
    secret_values: tuple[str, ...] = ()
    try:
        settings, secrets = _collect_init_settings()
        secret_values = tuple(secrets.values())
        document = _render_init_config(settings)
        candidate = load_settings_data(
            tomllib.loads(document),
            source_path=path,
            env_path=env_path,
            environ=secrets,
            validate_runtime=True,
        )
        probes = probe_model_components(candidate)
        failures = [result for result in probes if result.status is CheckStatus.FAIL]
        if failures:
            detail = "; ".join(f"{result.name}: {result.detail}" for result in failures)
            raise ConfigurationError(f"provider verification failed: {detail}")
    except (ConfigurationError, EOFError, OSError, ValueError) as error:
        safe_message = redact_secret_text(str(error), secret_values)
        print(f"初始化失败：{safe_message}", file=sys.stderr)
        raise SystemExit(1) from error

    env_existed = env_path.is_file()
    env_original = env_path.read_bytes() if env_existed else None
    try:
        merge_secret_file(env_path, secrets, force=force)
        _write_config_atomic(path, document)
    except (ConfigurationError, OSError, ValueError) as error:
        _restore_secret_file(env_path, env_existed, env_original)
        safe_message = redact_secret_text(str(error), secret_values)
        print(f"初始化失败：{safe_message}", file=sys.stderr)
        raise SystemExit(1) from error
    print(f"配置与模型服务验证完成：{path}")
    print(f"密钥已写入：{env_path}")


def handle_daily_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> bool:
    """执行 init 或 HTTP 日常命令；已处理时返回 True。"""
    if args.command == "init":
        initialize_config(
            args.config or Path("hl_mem.toml"),
            env_path=args.env_file or Path(".env"),
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
    if args.command == "correct":
        result = _request_json(
            args.url,
            "POST",
            f"/v1/memories/{args.memory_id}/correct",
            json_body={"corrected_text": args.text},
        )
        state = "已纠正记忆" if result.get("created", True) else "纠正已存在"
        print(
            f"{state} {args.memory_id}（新 Claim ID：{result.get('new_claim_id', '-')}；"
            f"纠正事件 ID：{result.get('correction_event_id', '-')}）。"
        )
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
