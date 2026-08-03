"""TOML 配置加载边界测试。"""

from __future__ import annotations

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
        llm_api_key="test-key"
    )


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
""",
    )

    settings = load_settings(config_path, tmp_path / ".env", environ={})

    assert settings.database_path == "custom.db"
    assert settings.database_pool_size == 4
    assert settings.llm_timeout == 12.0
    assert settings.query_expansion_model == "glm-4.7"
    assert settings.tag_channel_enabled is True
    assert settings.relevance_intents == ("current_state", "preference")
    assert settings.vector_backend is VectorBackend.SQLITE_SCAN
    assert settings.decay_min_confidence == 0.1


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
    assert settings.database_path == Settings().database_path


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
