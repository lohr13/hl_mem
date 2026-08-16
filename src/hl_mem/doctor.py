"""HL-Mem 安装与运行环境的只读诊断工具。"""

from __future__ import annotations

import argparse
import socket
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Mapping, Sequence

import httpx

from hl_mem.adapters.hermes.deployment import PLUGIN_FILES, plugin_files_match, plugin_files_present
from hl_mem.adapters.hermes.discovery import find_hermes_home
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
    settings = load_settings(config_path, env_path, environ=environ)
    resolved_database = database_path or Path(settings.database_path)
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
        _check_hermes(settings),
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
