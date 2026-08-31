"""HL-Mem 安装与运行环境的只读诊断工具。"""

from __future__ import annotations

import argparse
import json
import socket
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Sequence

import httpx

from hl_mem.adapters.hermes.deployment import PLUGIN_FILES, plugin_files_match, plugin_files_present
from hl_mem.adapters.hermes.discovery import find_hermes_home
from hl_mem.compatibility import (
    CONTEXT_PACKET_SCHEMA_MAJOR,
    DAEMON_CONTRACT_MAJOR,
    HERMES_PLUGIN_CONTRACT_MAJOR,
)
from hl_mem.config_loader import load_settings
from hl_mem.http_utils import retry_http
from hl_mem.ingest.embedder import Embedder
from hl_mem.settings import Settings, is_placeholder_secret

MIGRATION_DIR = Path(__file__).resolve().parent / "storage" / "migrations"


class CheckStatus(StrEnum):
    """诊断检查状态。"""

    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True)
class CheckResult:
    """单项诊断结果。"""

    status: CheckStatus
    name: str
    detail: str


@dataclass(frozen=True)
class DaemonProbe:
    """One read-only observation of the configured daemon health endpoint."""

    payload: Mapping[str, Any] | None
    error: str | None


def count_code_migrations(migration_dir: Path = MIGRATION_DIR) -> int:
    """统计代码目录中的 migration 数量（SQL 文件 + Python 数据 migration）。

    Python 数据 migration 通过文件内是否定义 ``DATA_MIGRATION_VERSION`` 来判定。
    """
    sql_count = sum(path.is_file() for path in migration_dir.glob("*.sql"))
    py_data_count = 0
    for path in migration_dir.glob("*.py"):
        if path.name == "__init__.py":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "DATA_MIGRATION_VERSION" in text:
            py_data_count += 1
    return sql_count + py_data_count


def _readonly_connection(database_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True)


def _check_database(database_path: Path) -> CheckResult:
    if not database_path.is_file():
        return CheckResult(CheckStatus.FAIL, "数据库文件", f"不存在：{database_path}")
    try:
        with _readonly_connection(database_path) as connection:
            connection.execute("SELECT 1").fetchone()
    except sqlite3.Error as error:
        return CheckResult(CheckStatus.FAIL, "数据库文件", f"无法只读打开：{error}")
    return CheckResult(CheckStatus.OK, "数据库文件", f"可只读打开：{database_path}")


def _check_migrations(database_path: Path) -> CheckResult:
    code_count = count_code_migrations()
    try:
        with _readonly_connection(database_path) as connection:
            applied_count = int(connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0])
    except sqlite3.Error as error:
        return CheckResult(CheckStatus.FAIL, "Migration 数量", f"无法读取；代码 {code_count} 个：{error}")
    status = CheckStatus.OK if applied_count == code_count else CheckStatus.FAIL
    return CheckResult(status, "Migration 数量", f"数据库 {applied_count} 个，代码 {code_count} 个")


def _check_fts_rebuild(database_path: Path) -> CheckResult:
    try:
        with tempfile.TemporaryDirectory(prefix="hl-mem-doctor-", ignore_cleanup_errors=True) as temporary_dir:
            copy_path = Path(temporary_dir) / "diagnostic.db"
            source = _readonly_connection(database_path)
            try:
                target = sqlite3.connect(copy_path)
                try:
                    source.backup(target)
                finally:
                    target.close()
            finally:
                source.close()
            connection = sqlite3.connect(copy_path)
            try:
                connection.execute("INSERT INTO claims_fts(claims_fts) VALUES ('rebuild')")
                connection.commit()
            finally:
                connection.close()
    except sqlite3.Error as error:
        return CheckResult(CheckStatus.FAIL, "claims_fts rebuild", f"临时副本测试失败：{error}")
    return CheckResult(CheckStatus.OK, "claims_fts rebuild", "临时副本测试成功，生产数据库未修改")


def _check_secrets(settings: Settings) -> CheckResult:
    """检查已启用组件对应的独立密钥。"""
    enabled: list[str] = []
    values = {
        "LLM_API_KEY": settings.llm_api_key,
        "EMBEDDING_API_KEY": settings.embedding_api_key,
        "RERANKER_API_KEY": settings.reranker_api_key,
        "IMAGE_API_KEY": settings.image_describer_api_key,
    }
    if (
        settings.extractor_mode != "fake"
        or settings.query_expansion_mode != "off"
        or settings.relation_discovery_mode != "off"
    ):
        enabled.append("LLM_API_KEY")
    if settings.embedder_mode == "real":
        enabled.append("EMBEDDING_API_KEY")
    if settings.reranker_mode in {"on", "real"}:
        enabled.append("RERANKER_API_KEY")
    if settings.image_describer_mode == "on":
        enabled.append("IMAGE_API_KEY")
    invalid = [name for name in enabled if is_placeholder_secret(values.get(name))]
    if invalid:
        return CheckResult(CheckStatus.FAIL, "密钥配置", f"缺失或为占位符：{', '.join(invalid)}")
    return CheckResult(CheckStatus.OK, "密钥配置", "已启用组件的独立密钥有效")


def _post(
    name: str,
    url: str,
    key: str | None,
    payload: dict[str, object],
    timeout: float,
    max_attempts: int,
) -> CheckResult:
    if is_placeholder_secret(key):
        return CheckResult(CheckStatus.WARN, name, "缺少有效 API key，跳过")
    try:

        def send_request() -> httpx.Response:
            response = httpx.post(
                url,
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
                timeout=timeout,
            )
            response.raise_for_status()
            return response

        response = retry_http(send_request, max_attempts=max_attempts)
    except (httpx.HTTPError, ValueError) as error:
        return CheckResult(CheckStatus.FAIL, name, f"最小请求失败：{error}")
    return CheckResult(CheckStatus.OK, name, f"请求成功（HTTP {response.status_code}）")


def _check_embedding(settings: Settings) -> CheckResult:
    if settings.embedder_mode == "fake":
        return CheckResult(CheckStatus.WARN, "Embedding API", "embedder=fake，跳过")
    try:
        Embedder(
            settings.embedding_api_key or "",
            settings.embedding_base_url,
            settings.embedding_model,
            settings.embedding_dim,
            settings.embedding_connect_timeout,
            settings.embedding_read_timeout,
            settings.embedding_max_attempts,
            api_mode=settings.embedding_api_mode,
            text_type=settings.embedding_text_type or None,
        ).embed_one("ping")
        return CheckResult(CheckStatus.OK, "Embedding API", "请求成功")
    except (RuntimeError, ValueError, KeyError, TypeError) as error:
        return CheckResult(CheckStatus.FAIL, "Embedding API", f"最小请求失败：{error}")


def _check_llm(settings: Settings) -> CheckResult:
    if settings.extractor_mode == "fake":
        return CheckResult(CheckStatus.WARN, "LLM API", "extractor=fake，跳过")
    base_url = settings.llm_base_url.rstrip("/")
    try:
        return _post(
            "LLM API",
            f"{base_url}/chat/completions",
            settings.llm_api_key,
            {
                "model": settings.llm_model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
            },
            settings.llm_timeout,
            settings.llm_max_attempts,
        )
    except ValueError as error:
        return CheckResult(CheckStatus.FAIL, "LLM API", f"配置值无效：{error}")


def _check_port() -> CheckResult:
    try:
        with socket.create_connection(("127.0.0.1", 8200), timeout=1.0):
            pass
    except OSError:
        return CheckResult(CheckStatus.WARN, "服务端口", "127.0.0.1:8200 未监听")
    return CheckResult(CheckStatus.OK, "服务端口", "127.0.0.1:8200 正在监听")


def _probe_daemon(settings: Settings) -> DaemonProbe:
    """Read health evidence once; this probe never mutates or negotiates state."""

    url = f"{settings.hermes_url.rstrip('/')}/healthz"
    try:
        response = httpx.get(url, timeout=1.0)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as error:
        return DaemonProbe(None, str(error))
    if not isinstance(payload, dict):
        return DaemonProbe(None, "healthz response is not a JSON object")
    return DaemonProbe(payload, None)


def _compatibility_section(probe: DaemonProbe) -> Mapping[str, Any] | None:
    if probe.payload is None:
        return None
    compatibility = probe.payload.get("compatibility")
    return compatibility if isinstance(compatibility, Mapping) else None


def _contract_int(value: object) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value


def _check_daemon_compatibility(probe: DaemonProbe) -> CheckResult:
    """Compare the observed daemon contract with this release's static major."""

    name = "Daemon 兼容性"
    if probe.payload is None:
        return CheckResult(CheckStatus.WARN, name, f"healthz 不可用：{probe.error or 'unknown error'}")
    compatibility = _compatibility_section(probe)
    if compatibility is None:
        return CheckResult(CheckStatus.FAIL, name, "healthz 缺少 compatibility 证据")
    observed_daemon = _contract_int(compatibility.get("daemon_contract_major"))
    observed_plugin = _contract_int(compatibility.get("required_plugin_contract_major"))
    mismatches = []
    if observed_daemon != DAEMON_CONTRACT_MAJOR:
        mismatches.append(f"daemon_contract_major={observed_daemon!r} (要求 {DAEMON_CONTRACT_MAJOR})")
    if observed_plugin != HERMES_PLUGIN_CONTRACT_MAJOR:
        mismatches.append(f"required_plugin_contract_major={observed_plugin!r} (要求 {HERMES_PLUGIN_CONTRACT_MAJOR})")
    if mismatches:
        return CheckResult(CheckStatus.FAIL, name, "；".join(mismatches))
    version = probe.payload.get("version", "unknown")
    return CheckResult(
        CheckStatus.OK,
        name,
        f"version={version}，daemon={observed_daemon} / required_plugin={observed_plugin}",
    )


def _check_wire_compatibility(probe: DaemonProbe) -> CheckResult:
    """Compare the daemon's Context Packet wire major with this consumer."""

    name = "Context Packet wire"
    if probe.payload is None:
        return CheckResult(CheckStatus.WARN, name, f"healthz 不可用：{probe.error or 'unknown error'}")
    compatibility = _compatibility_section(probe)
    context_packet = compatibility.get("context_packet") if compatibility is not None else None
    if not isinstance(context_packet, Mapping):
        return CheckResult(CheckStatus.FAIL, name, "healthz 缺少 context_packet contract 证据")
    observed_major = _contract_int(context_packet.get("schema_major"))
    observed_minor = _contract_int(context_packet.get("schema_minor"))
    if observed_major != CONTEXT_PACKET_SCHEMA_MAJOR:
        return CheckResult(
            CheckStatus.FAIL,
            name,
            f"schema_major={observed_major!r}，当前仅支持 {CONTEXT_PACKET_SCHEMA_MAJOR}",
        )
    if observed_minor is None:
        return CheckResult(CheckStatus.FAIL, name, "schema_minor 缺失或不是整数")
    return CheckResult(
        CheckStatus.OK,
        name,
        f"schema={observed_major}.{observed_minor}，major 兼容",
    )


def _check_plugin_compatibility(settings: Settings) -> CheckResult:
    """Read the installed plugin's static manifest without importing it."""

    name = "Hermes 插件兼容性"
    try:
        hermes_home = find_hermes_home(settings.hermes_home)
    except RuntimeError:
        return CheckResult(CheckStatus.WARN, name, "未检测到 Hermes，跳过")
    if not hermes_home.exists():
        return CheckResult(CheckStatus.WARN, name, "未检测到 Hermes，跳过")
    contract_path = hermes_home / "plugins" / "hl_mem" / "contract.json"
    if not contract_path.is_file():
        return CheckResult(CheckStatus.FAIL, name, f"缺少 {contract_path}；运行 hl-mem hermes upgrade")
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return CheckResult(CheckStatus.FAIL, name, f"contract.json 无法读取：{error}")
    if not isinstance(contract, dict):
        return CheckResult(CheckStatus.FAIL, name, "contract.json 必须是 JSON object")
    expected = {
        "plugin_contract_major": HERMES_PLUGIN_CONTRACT_MAJOR,
        "daemon_contract_major": DAEMON_CONTRACT_MAJOR,
        "context_packet_schema_major": CONTEXT_PACKET_SCHEMA_MAJOR,
    }
    mismatches = [
        f"{key}={contract.get(key)!r} (要求 {value})"
        for key, value in expected.items()
        if _contract_int(contract.get(key)) != value
    ]
    if mismatches:
        return CheckResult(CheckStatus.FAIL, name, "；".join(mismatches))
    return CheckResult(
        CheckStatus.OK,
        name,
        f"plugin={HERMES_PLUGIN_CONTRACT_MAJOR} / daemon={DAEMON_CONTRACT_MAJOR} / "
        f"context_packet={CONTEXT_PACKET_SCHEMA_MAJOR} major 兼容",
    )


def _check_hermes(settings: Settings) -> CheckResult:
    try:
        hermes_home = find_hermes_home(settings.hermes_home)
    except RuntimeError:
        return CheckResult(CheckStatus.WARN, "Hermes 插件", "未检测到 Hermes，跳过")
    if not hermes_home.exists():
        return CheckResult(CheckStatus.WARN, "Hermes 插件", "未检测到 Hermes，跳过")
    expected = hermes_home / "plugins" / "hl_mem"
    if plugin_files_match(expected):
        return CheckResult(CheckStatus.OK, "Hermes 插件", f"路径正确且无漂移：{expected}")
    if plugin_files_present(expected):
        return CheckResult(
            CheckStatus.FAIL,
            "Hermes 插件",
            "检测到插件副本漂移，运行 hl-mem hermes upgrade",
        )
    candidates = [
        hermes_home / "plugins" / "memory" / "hl_mem",
        hermes_home / "hermes-agent" / "plugins" / "hl_mem",
    ]
    actual = next((candidate for candidate in candidates if plugin_files_present(candidate)), None)
    if actual is not None:
        return CheckResult(CheckStatus.FAIL, "Hermes 插件", f"插件实际位于 {actual}；应安装到 {expected}")
    if any((expected / name).is_file() for name in PLUGIN_FILES):
        return CheckResult(
            CheckStatus.FAIL,
            "Hermes 插件",
            "检测到插件副本漂移，运行 hl-mem hermes upgrade",
        )
    return CheckResult(CheckStatus.FAIL, "Hermes 插件", f"应安装到 {expected}")


def run_doctor(
    database_path: Path | None = None,
    config_path: Path | None = None,
    env_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[CheckResult]:
    """执行全部诊断并返回结构化结果。"""
    settings = load_settings(config_path, env_path, environ=environ, validate_runtime=False)
    resolved_database = database_path or Path(settings.database_path)
    daemon_probe = _probe_daemon(settings)
    return [
        CheckResult(
            CheckStatus.OK if sys.version_info >= (3, 11) else CheckStatus.FAIL,
            "Python 版本",
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        ),
        _check_database(resolved_database),
        _check_migrations(resolved_database),
        _check_fts_rebuild(resolved_database),
        _check_secrets(settings),
        _check_embedding(settings),
        _check_llm(settings),
        _check_port(),
        _check_daemon_compatibility(daemon_probe),
        _check_hermes(settings),
        _check_plugin_compatibility(settings),
        _check_wire_compatibility(daemon_probe),
    ]


def main(argv: Sequence[str] | None = None) -> int:
    """运行 doctor 命令并打印逐项结果和汇总。"""
    parser = argparse.ArgumentParser(prog="hl-mem doctor")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args(argv)
    results = run_doctor(
        database_path=args.db,
        config_path=args.config,
        env_path=args.env_file,
    )
    for result in results:
        print(f"[{result.status}] {result.name} — {result.detail}")
    passed = sum(result.status is CheckStatus.OK for result in results)
    warnings_count = sum(result.status is CheckStatus.WARN for result in results)
    failures = sum(result.status is CheckStatus.FAIL for result in results)
    print(f"{passed} passed, {warnings_count} warnings, {failures} failures")
    return int(failures > 0)


if __name__ == "__main__":
    raise SystemExit(main())
