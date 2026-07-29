"""HL-Mem 安装与运行环境的只读诊断工具。"""

from __future__ import annotations

import argparse
import os
import socket
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Mapping, Sequence

import httpx

from hl_mem.http_utils import retry_http
from hl_mem.settings import is_placeholder_secret
from hl_mem.storage.database import default_database_path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
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


def read_env_file(path: Path) -> dict[str, str]:
    """读取简单 KEY=VALUE 格式环境文件，且不修改进程环境。"""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip("\"'")
    return values


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


def _check_env(env_path: Path, values: Mapping[str, str]) -> CheckResult:
    if not env_path.is_file():
        return CheckResult(CheckStatus.WARN, ".env 配置", f"文件不存在：{env_path}")
    enabled: list[str] = []
    if values.get("HL_MEM_EXTRACTOR", "fake").strip().lower() == "llm":
        enabled.append("LLM_API_KEY")
    if values.get("HL_MEM_EMBEDDER", "fake").strip().lower() == "real":
        enabled.append("EMBEDDING_API_KEY")
    if values.get("HL_MEM_RERANKER", "off").strip().lower() in {"on", "real"}:
        enabled.append("RERANKER_API_KEY")
    invalid = [name for name in enabled if is_placeholder_secret(values.get(name))]
    if invalid:
        return CheckResult(CheckStatus.FAIL, ".env 配置", f"缺失或为占位符：{', '.join(invalid)}")
    return CheckResult(CheckStatus.OK, ".env 配置", "已启用组件的关键配置有效")


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


def _check_embedding(values: Mapping[str, str]) -> CheckResult:
    if values.get("HL_MEM_EMBEDDER", "fake").strip().lower() == "fake":
        return CheckResult(CheckStatus.WARN, "Embedding API", "embedder=fake，跳过")
    base_url = values.get("EMBEDDING_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
    try:
        return _post(
            "Embedding API",
            f"{base_url}/embeddings",
            values.get("EMBEDDING_API_KEY"),
            {
                "model": values.get("EMBEDDING_MODEL", "text-embedding-v4"),
                "input": ["ping"],
                "dimensions": int(values.get("EMBEDDING_DIM", "2048")),
            },
            float(values.get("EMBEDDING_READ_TIMEOUT", "30")),
            int(values.get("EMBEDDING_MAX_ATTEMPTS", "3")),
        )
    except ValueError as error:
        return CheckResult(CheckStatus.FAIL, "Embedding API", f"配置值无效：{error}")


def _check_llm(values: Mapping[str, str]) -> CheckResult:
    if values.get("HL_MEM_EXTRACTOR", "fake").strip().lower() == "fake":
        return CheckResult(CheckStatus.WARN, "LLM API", "extractor=fake，跳过")
    base_url = values.get("LLM_BASE_URL", "https://coding.dashscope.aliyuncs.com/v1").rstrip("/")
    try:
        return _post(
            "LLM API",
            f"{base_url}/chat/completions",
            values.get("LLM_API_KEY"),
            {
                "model": values.get("LLM_MODEL", "glm-5.2"),
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
            },
            float(values.get("LLM_TIMEOUT", "30")),
            int(values.get("LLM_MAX_ATTEMPTS", "3")),
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


def _check_hermes(values: Mapping[str, str]) -> CheckResult:
    configured = values.get("HERMES_HOME")
    hermes_home = Path(configured).expanduser() if configured else Path.home() / ".hermes"
    if not hermes_home.exists():
        return CheckResult(CheckStatus.WARN, "Hermes 插件", "未检测到 Hermes，跳过")
    agent_home = hermes_home / "hermes-agent" if (hermes_home / "hermes-agent").is_dir() else hermes_home
    expected = agent_home / "plugins" / "hl_mem"
    if all((expected / name).is_file() for name in ("__init__.py", "plugin.yaml")):
        return CheckResult(CheckStatus.OK, "Hermes 插件", f"路径正确：{expected}")
    legacy = agent_home / "plugins" / "memory" / "hl_mem"
    suffix = f"；检测到旧错误路径 {legacy}" if legacy.exists() else ""
    return CheckResult(CheckStatus.FAIL, "Hermes 插件", f"应安装到 {expected}{suffix}")


def run_doctor(
    database_path: Path | None = None,
    env_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[CheckResult]:
    """执行全部诊断并返回结构化结果。"""
    resolved_env_path = env_path or PROJECT_ROOT / ".env"
    values = {**read_env_file(resolved_env_path), **dict(environ if environ is not None else os.environ)}
    resolved_database = database_path or Path(values.get("HL_MEM_DB_PATH", str(default_database_path())))
    return [
        CheckResult(
            CheckStatus.OK if sys.version_info >= (3, 11) else CheckStatus.FAIL,
            "Python 版本",
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        ),
        _check_database(resolved_database),
        _check_migrations(resolved_database),
        _check_fts_rebuild(resolved_database),
        _check_env(resolved_env_path, values),
        _check_embedding(values),
        _check_llm(values),
        _check_port(),
        _check_hermes(values),
    ]


def main(argv: Sequence[str] | None = None) -> int:
    """运行 doctor 命令并打印逐项结果和汇总。"""
    parser = argparse.ArgumentParser(prog="hl-mem doctor")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args(argv)
    results = run_doctor(database_path=args.db, env_path=args.env_file)
    for result in results:
        print(f"[{result.status}] {result.name} — {result.detail}")
    passed = sum(result.status is CheckStatus.OK for result in results)
    warnings_count = sum(result.status is CheckStatus.WARN for result in results)
    failures = sum(result.status is CheckStatus.FAIL for result in results)
    print(f"{passed} passed, {warnings_count} warnings, {failures} failures")
    return int(failures > 0)


if __name__ == "__main__":
    raise SystemExit(main())
