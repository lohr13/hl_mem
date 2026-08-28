"""TOML 配置加载边界测试。"""

from __future__ import annotations

import sys
from dataclasses import fields
from pathlib import Path

import pytest

from hl_mem.config_loader import load_settings
from hl_mem.errors import ConfigurationError
from hl_mem.settings import Settings, VectorBackend


def _write(path: Path, content: str = "") -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_missing_default_and_explicit_config_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigurationError, match=r"hl_mem\.toml.*does not exist"):
        load_settings(environ={})
    missing = tmp_path / "missing.toml"
    with pytest.raises(ConfigurationError, match=r"missing\.toml.*does not exist"):
        load_settings(missing, environ={})


def test_empty_toml_uses_static_defaults(tmp_path: Path) -> None:
    config_path = _write(tmp_path / "empty.toml")

    assert load_settings(config_path, tmp_path / ".env", environ={"LLM_API_KEY": "test-key"}) == Settings(
        database_path=str((tmp_path / "var" / "hl_mem.db").resolve()), llm_api_key="test-key"
    )


def test_old_toml_without_llm_max_tokens_keeps_default(tmp_path: Path) -> None:
    config_path = _write(
        tmp_path / "old.toml",
        '[recall]\nquery_expansion_mode = "off"\n',
    )

    settings = load_settings(config_path, tmp_path / ".env", environ={})

    assert settings.llm_max_tokens is None
    assert settings.llm_thinking_control == "auto"


def test_llm_max_tokens_loads_from_toml(tmp_path: Path) -> None:
    config_path = _write(
        tmp_path / "max-tokens.toml",
        '[llm]\nmax_tokens = 4000\n[recall]\nquery_expansion_mode = "off"\n',
    )

    settings = load_settings(config_path, tmp_path / ".env", environ={})

    assert settings.llm_max_tokens == 4000


def test_llm_thinking_control_loads_from_toml(tmp_path: Path) -> None:
    config_path = _write(
        tmp_path / "thinking-control.toml",
        '[llm]\nthinking_control = "chat_template_kwargs"\n' '[recall]\nquery_expansion_mode = "off"\n',
    )

    settings = load_settings(config_path, tmp_path / ".env", environ={})

    assert settings.llm_thinking_control == "chat_template_kwargs"


def test_release_example_config_matches_approved_modes() -> None:
    config_path = Path(__file__).parents[2] / "config.example.toml"

    settings = load_settings(
        config_path,
        config_path.with_name(".env.example"),
        environ={
            "LLM_API_KEY": "test-key",
            "EMBEDDING_API_KEY": "test-key",
            "RERANKER_API_KEY": "test-key",
            "IMAGE_API_KEY": "test-key",
        },
    )

    assert settings.conflict_auto_mode == "l0_only"
    assert settings.plan_fulfillment_mode == "enforce"
    assert settings.price_target_mode == "enforce"
    assert settings.hermes_manual_conflict_notice is True
    assert settings.dedup_audit_only is True
    assert settings.lesson_signal_mode == "observe"
    assert settings.entity_constraint_mode == "observe"


def test_hermes_on_demand_recall_timeout_loads_from_toml(tmp_path: Path) -> None:
    config_path = _write(
        tmp_path / "hermes-timeout.toml",
        "[hermes]\non_demand_recall_timeout_seconds = 6.5\n" "[recall]\nquery_expansion_mode = 'off'\n",
    )

    settings = load_settings(config_path, tmp_path / ".env", environ={})

    assert settings.hermes_on_demand_recall_timeout_seconds == 6.5


def test_extraction_soft_split_flag_loads_from_toml_and_defaults_off(tmp_path: Path) -> None:
    config_path = _write(
        tmp_path / "soft-split.toml",
        "[extraction]\nsoft_split_enabled = true\n" "[recall]\nquery_expansion_mode = 'off'\n",
    )

    settings = load_settings(config_path, tmp_path / ".env", environ={})

    assert Settings().extraction_soft_split_enabled is False
    assert settings.extraction_soft_split_enabled is True


def test_extraction_delta_repair_flag_loads_from_toml_and_defaults_off(tmp_path: Path) -> None:
    config_path = _write(
        tmp_path / "delta-repair.toml",
        "[extraction]\ndelta_repair_enabled = true\n" "[recall]\nquery_expansion_mode = 'off'\n",
    )

    settings = load_settings(config_path, tmp_path / ".env", environ={})

    assert Settings().extraction_delta_repair_enabled is False
    assert settings.extraction_delta_repair_enabled is True


def test_old_config_without_lifecycle_keys_adopts_v027_defaults(tmp_path: Path) -> None:
    config_path = _write(
        tmp_path / "old.toml",
        """
[recall]
query_expansion_mode = "off"
""",
    )

    settings = load_settings(config_path, tmp_path / ".env", environ={})

    assert settings.resurrection_mode == "auto"
    assert settings.decay_model == "activation_halflife"


def test_old_lifecycle_behavior_remains_available_by_explicit_config(tmp_path: Path) -> None:
    config_path = _write(
        tmp_path / "legacy.toml",
        """
[recall]
query_expansion_mode = "off"
resurrection_mode = "off"

[decay]
model = "legacy_linear"
""",
    )

    settings = load_settings(config_path, tmp_path / ".env", environ={})

    assert settings.resurrection_mode == "off"
    assert settings.decay_model == "legacy_linear"


def test_loads_native_types_tuple_and_string_enum(tmp_path: Path) -> None:
    config_path = _write(
        tmp_path / "all-types.toml",
        """
[database]
path = "custom.db"
pool_size = 4

[llm]
timeout = 12

[recall]
query_expansion_model = "glm-4.7"
query_expansion_mode = "off"
tag_channel_enabled = true
relevance_intents = ["current_state", "preference"]
vector_backend = "sqlite_scan"

[retention]
decay_min_confidence = 0.1

[state]
latest_wins_mode = "enforce"
latest_wins_slots = ["config.version"]
""",
    )

    settings = load_settings(config_path, tmp_path / ".env", environ={})

    assert settings.database_path == str((tmp_path / "custom.db").resolve())
    assert settings.database_pool_size == 4
    assert settings.llm_timeout == 12.0
    assert settings.query_expansion_model == "glm-4.7"
    assert settings.tag_channel_enabled is True
    assert settings.relevance_intents == ("current_state", "preference")
    assert settings.vector_backend is VectorBackend.SQLITE_SCAN
    assert settings.decay_min_confidence == 0.1
    assert settings.latest_wins_mode == "enforce"
    assert settings.latest_wins_slots == ("config.version",)


def test_latest_wins_toml_cannot_authorize_a_slot_outside_the_code_allowlist(tmp_path: Path) -> None:
    config_path = _write(
        tmp_path / "invalid-state.toml",
        '[state]\nlatest_wins_slots = ["config.version", "config.other"]\n' '[recall]\nquery_expansion_mode = "off"\n',
    )

    with pytest.raises(ConfigurationError, match=r"state\.latest_wins_slots.*config\.version"):
        load_settings(config_path, tmp_path / ".env", environ={})


@pytest.mark.parametrize(
    ("content", "key_path", "problem"),
    [
        ("[unknown]\nvalue = 1\n", "unknown", "unknown TOML table"),
        ("[database]\nunknown = 1\n", "database.unknown", "unknown TOML key"),
        ('[database]\npool_size = "eight"\n', "database.pool_size", "expected int"),
        ("[database\npath = 'x'\n", "", "invalid TOML"),
        ('[llm]\napi_key = "top-secret"\n', "llm.api_key", "secrets must not appear"),
        ('LLM_API_KEY = "top-secret"\n', "LLM_API_KEY", "secrets must not appear"),
    ],
)
def test_structural_errors_include_path_and_full_key(
    tmp_path: Path,
    content: str,
    key_path: str,
    problem: str,
) -> None:
    config_path = _write(tmp_path / "invalid.toml", content)

    with pytest.raises(ConfigurationError) as caught:
        load_settings(config_path, environ={})

    message = str(caught.value)
    assert str(config_path) in message
    assert problem in message
    if key_path:
        assert key_path in message
    assert "top-secret" not in message


def test_dotenv_secrets_are_overridden_only_by_same_process_names(tmp_path: Path) -> None:
    config_path = _write(tmp_path / "config.toml")
    env_path = _write(
        tmp_path / ".env",
        "\n".join(
            (
                "LLM_API_KEY=dotenv-llm",
                "EMBEDDING_API_KEY=dotenv-embedding",
                "RERANKER_API_KEY=dotenv-reranker",
                "IMAGE_API_KEY=dotenv-image",
                "HL_MEM_DB_PATH=ignored.db",
            )
        ),
    )

    settings = load_settings(
        config_path,
        env_path,
        environ={
            "LLM_API_KEY": "process-llm",
            "HL_MEM_DB_PATH": "also-ignored.db",
        },
    )

    assert settings.llm_api_key == "process-llm"
    assert settings.embedding_api_key == "dotenv-embedding"
    assert settings.reranker_api_key == "dotenv-reranker"
    assert settings.image_describer_api_key == "dotenv-image"
    assert settings.image_describer_api_key != settings.llm_api_key
    assert settings.database_path == str((tmp_path / "var" / "hl_mem.db").resolve())


def test_relative_database_path_uses_real_config_target_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置 symlink 的位置不得把相对数据库路径拉回宿主 CWD。"""
    canonical_dir = tmp_path / "canonical"
    source_dir = tmp_path / "source"
    canonical_dir.mkdir()
    source_dir.mkdir()
    canonical_config = _write(
        canonical_dir / "hl_mem.toml",
        '[database]\npath = "data/memory.db"\n[recall]\nquery_expansion_mode = "off"\n',
    )
    linked_config = tmp_path / "hl_mem.toml"
    linked_config.symlink_to(canonical_config)
    monkeypatch.chdir(source_dir)

    settings = load_settings(linked_config, tmp_path / ".env", environ={})

    assert settings.database_path == str((canonical_dir / "data" / "memory.db").resolve())


@pytest.mark.parametrize("foreign_path", ["D:/hl_mem/var/hl_mem.db", r"\\server\share\hl_mem.db"])
def test_posix_rejects_windows_absolute_database_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    foreign_path: str,
) -> None:
    """POSIX 启动不得把 Windows drive 或 UNC 路径静默当成相对目录。"""
    escaped_path = foreign_path.replace("\\", "\\\\")
    config_path = _write(
        tmp_path / "hl_mem.toml",
        f'[database]\npath = "{escaped_path}"\n[recall]\nquery_expansion_mode = "off"\n',
    )
    monkeypatch.setattr(sys, "platform", "linux")

    with pytest.raises(ConfigurationError, match=r"database\.path.*Windows absolute path.*POSIX"):
        load_settings(config_path, tmp_path / ".env", environ={})


def test_windows_rejects_posix_absolute_database_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows 启动不得把 POSIX 根路径映射到当前盘符。"""
    config_path = _write(
        tmp_path / "hl_mem.toml",
        '[database]\npath = "/root/hl_mem/var/hl_mem.db"\n[recall]\nquery_expansion_mode = "off"\n',
    )
    monkeypatch.setattr(sys, "platform", "win32")

    with pytest.raises(ConfigurationError, match=r"database\.path.*POSIX absolute path.*Windows"):
        load_settings(config_path, tmp_path / ".env", environ={})


@pytest.mark.parametrize(
    ("platform", "raw_path", "expected"),
    [
        ("win32", "D:/hl_mem/var/hl_mem.db", "D:/hl_mem/var/hl_mem.db"),
        ("win32", r"\\server\share\hl_mem.db", r"\\server\share\hl_mem.db"),
        ("linux", "/srv/hl_mem/var/hl_mem.db", "/srv/hl_mem/var/hl_mem.db"),
        ("linux", "//srv/hl_mem/var/hl_mem.db", "//srv/hl_mem/var/hl_mem.db"),
        ("linux", "///srv/hl_mem/var/hl_mem.db", "///srv/hl_mem/var/hl_mem.db"),
    ],
)
def test_native_absolute_database_path_is_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    raw_path: str,
    expected: str,
) -> None:
    """本平台原生绝对路径只做词法规范化，不改变目标。"""
    escaped_path = raw_path.replace("\\", "\\\\")
    config_path = _write(
        tmp_path / "hl_mem.toml",
        f'[database]\npath = "{escaped_path}"\n[recall]\nquery_expansion_mode = "off"\n',
    )
    monkeypatch.setattr(sys, "platform", platform)

    settings = load_settings(config_path, tmp_path / ".env", environ={})

    assert settings.database_path == expected


def test_placeholder_error_is_redacted(tmp_path: Path) -> None:
    config_path = _write(tmp_path / "enabled.toml", "[extraction]\nmode = 'real'\n")
    env_path = _write(tmp_path / ".env", "LLM_API_KEY=sk-xxx\n")

    with pytest.raises(ConfigurationError) as caught:
        load_settings(config_path, env_path, environ={})

    message = str(caught.value)
    assert str(config_path) in message
    assert "LLM_API_KEY" in message
    assert "sk-xxx" not in message


def test_missing_enabled_component_keys_explain_env_and_toml_recovery(tmp_path: Path) -> None:
    config_path = _write(
        tmp_path / "enabled.toml",
        """
[extraction]
mode = "real"
[embedding]
mode = "real"
[reranker]
mode = "on"
[image_describer]
mode = "on"
[recall]
query_expansion_mode = "off"
[relation]
discovery_mode = "off"
""".strip(),
    )

    with pytest.raises(ConfigurationError) as caught:
        load_settings(config_path, tmp_path / ".env", environ={})

    message = str(caught.value)
    for secret_name in ("LLM_API_KEY", "EMBEDDING_API_KEY", "RERANKER_API_KEY", "IMAGE_API_KEY"):
        assert secret_name in message
    assert ".env" in message
    assert "extraction.mode='fake'" in message
    assert "embedding.mode='fake'" in message
    assert "reranker.mode='off'" in message
    assert "image_describer.mode='off'" in message


def test_every_settings_field_declares_exactly_one_source() -> None:
    for settings_field in fields(Settings):
        assert set(settings_field.metadata) in ({"toml"}, {"secret_env"})
