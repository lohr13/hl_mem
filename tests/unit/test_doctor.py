"""doctor 诊断命令的单元测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hl_mem.doctor import CheckResult, CheckStatus, count_code_migrations, run_doctor
from hl_mem.errors import ConfigurationError
from hl_mem.settings import Settings, is_placeholder_secret
from hl_mem.storage.database import Database


def test_doctor_runs_without_crashing(tmp_path: Path, monkeypatch) -> None:
    """离线配置下 doctor 应完整返回所有检查结果。"""
    database_path = tmp_path / "doctor.db"
    database = Database(database_path)
    database.open()
    database.close()
    config_path = tmp_path / "hl_mem.toml"
    config_path.write_text('[recall]\nquery_expansion_mode = "off"\n', encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "hl_mem.doctor._check_port",
        lambda: CheckResult(CheckStatus.WARN, "服务端口", "测试跳过"),
    )
    results = run_doctor(
        database_path=database_path,
        config_path=config_path,
        env_path=env_path,
        environ={},
    )
    assert len(results) == 9


def test_placeholder_secret_detection() -> None:
    """空值和常见模板值应被识别，真实格式密钥不应误报。"""
    for value in (None, "", "xxx", "your-key", "<API_KEY>", "sk-xxx", "sk-e72xxx"):
        assert is_placeholder_secret(value)
    assert not is_placeholder_secret("sk-live-abcdef123456")


def test_migration_count_matches_database(tmp_path: Path) -> None:
    """全新数据库应用的 migration 数应与代码文件数一致。"""
    database_path = tmp_path / "migrations.db"
    database = Database(database_path)
    database.open()
    database.close()
    with sqlite3.connect(database_path) as connection:
        applied = int(connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0])
    assert applied == count_code_migrations()


def test_enabled_components_reject_placeholder_secrets() -> None:
    """启用真实组件时，任何环境都必须拒绝空值或占位密钥。"""
    settings = Settings(
        extractor_mode="llm",
        embedder_mode="real",
        reranker_mode="on",
        llm_api_key="sk-xxx",
        embedding_api_key="embedding-real-key",
        reranker_api_key="reranker-real-key",
    )
    with pytest.raises(ConfigurationError, match="LLM_API_KEY"):
        settings.validate()

    with pytest.raises(ConfigurationError, match="LLM_API_KEY"):
        Settings(extractor_mode="real", llm_api_key="your-key").validate()
