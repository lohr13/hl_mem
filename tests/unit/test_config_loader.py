"""TOML 配置加载边界测试。"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import fields
from pathlib import Path

import pytest

from hl_mem.config_loader import load_settings
from hl_mem.errors import ConfigurationError
from hl_mem.settings import Settings, VectorBackend
from scripts.check_config_schema_snapshot import build_config_schema


def _write(path: Path, content: str = "", *, versioned: bool = True) -> Path:
    if versioned and path.suffix == ".toml" and not content.lstrip().startswith("schema_version"):
        content = _v1(content)
    path.write_text(content, encoding="utf-8")
    return path


def _v1(body: str = "") -> str:
    return "schema_version = 1\n" + body


def _load_structural_settings(
    config_path: Path | None = None,
    env_path: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    return load_settings(config_path, env_path, environ=environ, validate_runtime=False)


def _load_runtime_settings(
    config_path: Path | None = None,
    env_path: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    return load_settings(config_path, env_path, environ=environ, validate_runtime=True)


def test_public_config_schema_is_secret_free_and_production_only() -> None:
    schema = build_config_schema()
    fields = {item["path"]: item for item in schema["fields"]}

    assert schema["schema_version"] == 1
    assert "plugins.<id>" in schema["open_namespaces"]
    assert "recall.tag_channel_enabled" in schema["retired_paths"]
    assert "fake" not in fields["extraction.mode"]["production_choices"]
    assert "fake" not in fields["embedding.mode"]["production_choices"]
    assert "fake" not in fields["reranker.mode"]["production_choices"]
    assert fields["recall.entity_constraint_mode"]["default"] == "enforce"
    assert all(set(item) == {"environment", "settings_field"} for item in schema["secrets"])


def test_unversioned_config_requires_migration(tmp_path: Path) -> None:
    path = _write(tmp_path / "legacy.toml", "[database]\npath='memory.db'\n", versioned=False)

    with pytest.raises(ConfigurationError, match=r"schema_version.*hl-mem config migrate"):
        _load_structural_settings(path, environ={})


def test_future_config_fails_without_guessing(tmp_path: Path) -> None:
    path = _write(tmp_path / "future.toml", "schema_version = 2\n")

    with pytest.raises(ConfigurationError, match=r"unsupported schema_version 2"):
        _load_structural_settings(path, environ={})


def test_v1_production_profile_loads_with_explicit_model_services(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "production.toml",
        _v1("""
[llm]
provider = "openai_compatible"
base_url = "https://llm.example.test/v1"
model = "quality-llm"

[extraction]
mode = "llm"

[embedding]
mode = "real"
base_url = "https://embedding.example.test/v1"
model = "quality-embedding"
dim = 2048
api_mode = "compatible"
"""),
    )

    settings = _load_runtime_settings(
        path,
        tmp_path / ".env",
        environ={"LLM_API_KEY": "llm-secret", "EMBEDDING_API_KEY": "embedding-secret"},
    )

    assert settings.schema_version == 1
    assert settings.extractor_mode == "llm"
    assert settings.embedder_mode == "real"


def test_v1_production_profile_does_not_inherit_provider_defaults(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "implicit-provider.toml",
        _v1('[extraction]\nmode = "llm"\n[embedding]\nmode = "real"\n'),
    )

    with pytest.raises(ConfigurationError, match=r"explicitly set.*llm\.provider.*embedding\.base_url"):
        _load_runtime_settings(path, environ={})


@pytest.mark.parametrize(
    ("section", "mode"),
    (("extraction", "fake"), ("embedding", "fake")),
)
def test_v1_production_profile_rejects_fake_modes(tmp_path: Path, section: str, mode: str) -> None:
    extraction_mode = mode if section == "extraction" else "llm"
    embedding_mode = mode if section == "embedding" else "real"
    path = _write(
        tmp_path / f"fake-{section}.toml",
        _v1(f"""
[llm]
provider = "openai_compatible"
base_url = "https://llm.example.test/v1"
model = "quality-llm"

[extraction]
mode = "{extraction_mode}"

[embedding]
mode = "{embedding_mode}"
base_url = "https://embedding.example.test/v1"
model = "quality-embedding"
dim = 2048
api_mode = "compatible"
"""),
    )

    with pytest.raises(ConfigurationError, match=rf"{section}\.mode.*Fake|Fake.*{section}\.mode"):
        _load_runtime_settings(
            path,
            tmp_path / ".env",
            environ={"LLM_API_KEY": "llm-secret", "EMBEDDING_API_KEY": "embedding-secret"},
        )


def test_test_factory_is_the_only_fake_profile() -> None:
    settings = Settings.for_test()

    assert settings.extractor_mode == "fake"
    assert settings.embedder_mode == "fake"
    settings.validate()


@pytest.mark.parametrize("mode", ("off", "observe"))
def test_entity_constraint_preserves_explicit_non_default_mode(tmp_path: Path, mode: str) -> None:
    path = _write(tmp_path / f"entity-{mode}.toml", f'[recall]\nentity_constraint_mode = "{mode}"\n')

    settings = _load_structural_settings(path, environ={})

    assert settings.entity_constraint_mode == mode


def test_semantic_automation_defaults_are_explicit_and_safe() -> None:
    settings = Settings.for_test()

    assert settings.dedup_enabled is True
    assert settings.dedup_llm_enabled is False
    assert settings.semantic_conflict_consolidation_enabled is False
    assert settings.policy_induction_enabled is False
    assert settings.reclassify_enabled is False


def test_v1_plugin_namespace_is_typed_and_immutable(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "plugins.toml",
        _v1("""
[plugins]
enabled = ["example-reranker"]

[plugins.example-reranker]
threshold = 0.75
labels = ["trusted", "local"]
"""),
    )

    settings = _load_structural_settings(path, environ={})

    assert settings.plugins_enabled == ("example-reranker",)
    assert settings.plugin_options["example-reranker"]["threshold"] == 0.75
    assert settings.plugin_options["example-reranker"]["labels"] == ("trusted", "local")
    with pytest.raises(TypeError):
        settings.plugin_options["example-reranker"]["threshold"] = 0.5  # type: ignore[index]


@pytest.mark.parametrize(
    "body",
    (
        '[plugins]\nenabled = ["duplicate", "duplicate"]\n',
        '[plugins]\nenabled = ["Bad ID"]\n',
        '[plugins."Bad ID"]\nvalue = true\n',
    ),
)
def test_v1_plugin_namespace_fails_closed_for_invalid_ids(tmp_path: Path, body: str) -> None:
    path = _write(tmp_path / "invalid-plugin.toml", _v1(body))

    with pytest.raises(ConfigurationError, match="plugins"):
        _load_structural_settings(path, environ={})


@pytest.mark.parametrize("key", ("api_token", "API-KEY", "nested_secret", "authorization"))
def test_v1_plugin_namespace_rejects_nested_secret_options(tmp_path: Path, key: str) -> None:
    path = _write(
        tmp_path / "plugin-secret.toml",
        _v1(f"""\n[plugins]\nenabled = ["vendor.plugin"]\n\n[plugins.vendor.plugin.nested]\n{key} = "do-not-log"\n"""),
    )

    with pytest.raises(ConfigurationError, match=r"plugins\.vendor\.plugin\.nested") as captured:
        _load_structural_settings(path, environ={})

    assert "do-not-log" not in str(captured.value)


def test_v1_accepts_external_provider_names_and_embedding_provider(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "external-providers.toml",
        _v1("""
[llm]
provider = "vendor_llm"

[embedding]
provider = "vendor_embedding"

[reranker]
provider = "vendor_reranker"

[image_describer]
provider = "vendor_image"
"""),
    )

    settings = _load_structural_settings(path, environ={})

    assert settings.llm_provider == "vendor_llm"
    assert settings.embedding_provider == "vendor_embedding"
    assert settings.reranker_provider == "vendor_reranker"
    assert settings.image_describer_provider == "vendor_image"


def test_v1_loads_usage_governance_limits(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "usage.toml",
        _v1("""
[usage]
daily_request_limit = 200
daily_cost_limit_microunits = 500000
reservation_lease_seconds = 120
"""),
    )

    settings = _load_structural_settings(path, environ={})

    assert settings.usage_daily_request_limit == 200
    assert settings.usage_daily_cost_limit_microunits == 500_000
    assert settings.usage_reservation_lease_seconds == 120


def test_v1_resolves_optional_price_book_relative_to_the_config_without_exposing_path(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    path = _write(
        config_dir / "hl_mem.toml",
        _v1('[usage]\nprice_book_path = "pricing/provider.json"\n'),
    )

    settings = _load_structural_settings(path, environ={})

    assert settings.usage_price_book_path == str((config_dir / "pricing" / "provider.json").resolve())
    snapshot = settings.snapshot()
    assert snapshot["price_book_configured"] is True
    assert "usage_price_book_path" not in snapshot
    assert str(config_dir) not in repr(snapshot)


def test_v1_without_price_book_keeps_the_existing_optional_config_contract(tmp_path: Path) -> None:
    settings = _load_structural_settings(_write(tmp_path / "hl_mem.toml"), environ={})

    assert settings.usage_price_book_path is None
    assert settings.snapshot()["price_book_configured"] is False


def test_public_config_schema_adds_only_the_optional_price_book_path() -> None:
    schema = build_config_schema()
    pricing_fields = [item for item in schema["fields"] if item["path"].startswith("usage.price_book")]

    assert pricing_fields == [
        {
            "default": None,
            "path": "usage.price_book_path",
            "required_in_production": False,
            "settings_field": "usage_price_book_path",
            "type": "string",
        }
    ]


@pytest.mark.parametrize(
    "body",
    (
        '[llm]\nprovider = "Bad Provider"\n',
        '[embedding]\nprovider = "Bad Provider"\n',
        '[reranker]\nprovider = "Bad Provider"\n',
        '[image_describer]\nprovider = "Bad Provider"\n',
    ),
)
def test_v1_rejects_invalid_provider_names(tmp_path: Path, body: str) -> None:
    path = _write(tmp_path / "invalid-provider.toml", _v1(body))

    with pytest.raises(ConfigurationError, match="invalid provider name"):
        _load_structural_settings(path, environ={})


@pytest.mark.parametrize(
    "body",
    (
        "[extraction]\npre_filter = true\n",
        "[recall]\ntag_channel_enabled = true\n",
        "[recall]\ntag_channel_weight = 0.2\n",
        "[recall]\ntag_candidate_limit = 10\n",
        "[relation]\nauto_apply_confidence = 0.9\n",
        "[relation]\nconflict_confidence = 0.8\n",
    ),
)
def test_v1_rejects_retired_surfaces_with_migration_command(tmp_path: Path, body: str) -> None:
    path = _write(tmp_path / "retired.toml", body)

    with pytest.raises(ConfigurationError, match=r"retired.*hl-mem config migrate"):
        _load_structural_settings(path, environ={})


def test_missing_default_and_explicit_config_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigurationError, match=r"hl_mem\.toml.*does not exist"):
        _load_structural_settings(environ={})
    missing = tmp_path / "missing.toml"
    with pytest.raises(ConfigurationError, match=r"missing\.toml.*does not exist"):
        _load_structural_settings(missing, environ={})


def test_empty_toml_uses_static_defaults(tmp_path: Path) -> None:
    config_path = _write(tmp_path / "empty.toml")

    assert _load_structural_settings(config_path, tmp_path / ".env", environ={"LLM_API_KEY": "test-key"}) == Settings(
        database_path=str((tmp_path / "var" / "hl_mem.db").resolve()), llm_api_key="test-key"
    )


def test_negative_max_request_body_is_rejected_from_toml(tmp_path: Path) -> None:
    config_path = _write(
        tmp_path / "negative-request-limit.toml",
        "[server]\nmax_request_body = -1\n[recall]\nquery_expansion_mode = 'off'\n",
    )

    with pytest.raises(ConfigurationError) as caught:
        _load_structural_settings(config_path, tmp_path / ".env", environ={})

    assert str(caught.value) == f"{config_path}: server.max_request_body must be non-negative"


def test_zero_max_request_body_loads_from_toml(tmp_path: Path) -> None:
    config_path = _write(
        tmp_path / "zero-request-limit.toml",
        "[server]\nmax_request_body = 0\n[recall]\nquery_expansion_mode = 'off'\n",
    )

    assert _load_structural_settings(config_path, tmp_path / ".env", environ={}).max_request_body == 0


def test_old_toml_without_llm_max_tokens_keeps_default(tmp_path: Path) -> None:
    config_path = _write(
        tmp_path / "old.toml",
        '[recall]\nquery_expansion_mode = "off"\n',
    )

    settings = _load_structural_settings(config_path, tmp_path / ".env", environ={})

    assert settings.llm_max_tokens is None
    assert settings.llm_reasoning_effort is None
    assert settings.llm_thinking_control == "auto"


def test_llm_max_tokens_loads_from_toml(tmp_path: Path) -> None:
    config_path = _write(
        tmp_path / "max-tokens.toml",
        '[llm]\nmax_tokens = 4000\n[recall]\nquery_expansion_mode = "off"\n',
    )

    settings = _load_structural_settings(config_path, tmp_path / ".env", environ={})

    assert settings.llm_max_tokens == 4000


def test_llm_reasoning_effort_loads_from_toml(tmp_path: Path) -> None:
    config_path = _write(
        tmp_path / "reasoning-effort.toml",
        '[llm]\nreasoning_effort = "low"\n[recall]\nquery_expansion_mode = "off"\n',
    )

    settings = _load_structural_settings(config_path, tmp_path / ".env", environ={})

    assert settings.llm_reasoning_effort == "low"


def test_llm_thinking_control_loads_from_toml(tmp_path: Path) -> None:
    config_path = _write(
        tmp_path / "thinking-control.toml",
        '[llm]\nthinking_control = "chat_template_kwargs"\n' '[recall]\nquery_expansion_mode = "off"\n',
    )

    settings = _load_structural_settings(config_path, tmp_path / ".env", environ={})

    assert settings.llm_thinking_control == "chat_template_kwargs"


def test_release_example_config_matches_approved_modes() -> None:
    config_path = Path(__file__).parents[2] / "config.example.toml"

    settings = _load_runtime_settings(
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
    assert settings.entity_constraint_mode == "enforce"


@pytest.mark.parametrize("retired_mode", ("observe", "enforce"))
def test_retired_conflict_auto_modes_are_rejected_fail_closed(tmp_path: Path, retired_mode: str) -> None:
    config_path = _write(
        tmp_path / f"retired-{retired_mode}.toml",
        f'[conflict]\nauto_mode = "{retired_mode}"\n',
    )

    with pytest.raises(ConfigurationError) as caught:
        _load_structural_settings(config_path, tmp_path / ".env", environ={})

    assert str(caught.value) == f"{config_path}: conflict.auto_mode: expected one of 'off', 'l0_only'"


def test_retired_maintenance_judge_table_is_rejected_fail_closed(tmp_path: Path) -> None:
    config_path = _write(
        tmp_path / "retired-maintenance-judge.toml",
        '[maintenance_judge]\nbase_url = "http://127.0.0.1:8090/v1"\n',
    )

    with pytest.raises(ConfigurationError) as caught:
        _load_structural_settings(config_path, tmp_path / ".env", environ={})

    assert str(caught.value) == f"{config_path}: maintenance_judge: unknown TOML table"


@pytest.mark.parametrize("active_mode", ("off", "l0_only"))
def test_supported_conflict_auto_modes_still_load(tmp_path: Path, active_mode: str) -> None:
    config_path = _write(
        tmp_path / f"supported-{active_mode}.toml",
        f'[conflict]\nauto_mode = "{active_mode}"\n[recall]\nquery_expansion_mode = "off"\n',
    )

    assert _load_structural_settings(config_path, tmp_path / ".env", environ={}).conflict_auto_mode == active_mode


def test_hermes_on_demand_recall_timeout_loads_from_toml(tmp_path: Path) -> None:
    config_path = _write(
        tmp_path / "hermes-timeout.toml",
        "[hermes]\non_demand_recall_timeout_seconds = 6.5\n" "[recall]\nquery_expansion_mode = 'off'\n",
    )

    settings = _load_structural_settings(config_path, tmp_path / ".env", environ={})

    assert settings.hermes_on_demand_recall_timeout_seconds == 6.5


def test_extraction_soft_split_flag_loads_from_toml_and_defaults_off(tmp_path: Path) -> None:
    config_path = _write(
        tmp_path / "soft-split.toml",
        "[extraction]\nsoft_split_enabled = true\n" "[recall]\nquery_expansion_mode = 'off'\n",
    )

    settings = _load_structural_settings(config_path, tmp_path / ".env", environ={})

    assert Settings().extraction_soft_split_enabled is False
    assert settings.extraction_soft_split_enabled is True


def test_extraction_delta_repair_flag_loads_from_toml_and_defaults_off(tmp_path: Path) -> None:
    config_path = _write(
        tmp_path / "delta-repair.toml",
        "[extraction]\ndelta_repair_enabled = true\n" "[recall]\nquery_expansion_mode = 'off'\n",
    )

    settings = _load_structural_settings(config_path, tmp_path / ".env", environ={})

    assert Settings().extraction_delta_repair_enabled is False
    assert settings.extraction_delta_repair_enabled is True


def test_v1_config_without_lifecycle_keys_uses_safe_defaults(tmp_path: Path) -> None:
    config_path = _write(
        tmp_path / "old.toml",
        """
[recall]
query_expansion_mode = "off"
""",
    )

    settings = _load_structural_settings(config_path, tmp_path / ".env", environ={})

    assert settings.resurrection_mode == "off"
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

    settings = _load_structural_settings(config_path, tmp_path / ".env", environ={})

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
relevance_intents = ["current_state", "preference"]
vector_backend = "sqlite_scan"

[retention]
decay_min_confidence = 0.1

[state]
latest_wins_mode = "enforce"
latest_wins_slots = ["config.version"]
""",
    )

    settings = _load_structural_settings(config_path, tmp_path / ".env", environ={})

    assert settings.database_path == str((tmp_path / "custom.db").resolve())
    assert settings.database_pool_size == 4
    assert settings.llm_timeout == 12.0
    assert settings.query_expansion_model == "glm-4.7"
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
        _load_structural_settings(config_path, tmp_path / ".env", environ={})


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
        _load_structural_settings(config_path, environ={})

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

    settings = _load_structural_settings(
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


def test_query_expansion_line_loads_from_recall_and_generic_secret_env(tmp_path: Path) -> None:
    config_path = _write(
        tmp_path / "config.toml",
        "\n".join(
            (
                "[recall]",
                'query_expansion_provider = "dashscope"',
                'query_expansion_base_url = "https://qe.example.com/v1"',
            )
        ),
    )
    env_path = _write(tmp_path / ".env", "QUERY_EXPANSION_API_KEY=qe-secret")

    settings = _load_structural_settings(config_path, env_path, environ={})

    assert settings.query_expansion_provider == "dashscope"
    assert settings.query_expansion_base_url == "https://qe.example.com/v1"
    assert settings.query_expansion_api_key == "qe-secret"


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

    settings = _load_structural_settings(linked_config, tmp_path / ".env", environ={})

    assert settings.database_path == str((canonical_dir / "data" / "memory.db").resolve())


def test_relative_database_and_price_book_paths_share_real_config_target_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical_dir = tmp_path / "canonical"
    source_dir = tmp_path / "source"
    canonical_dir.mkdir()
    source_dir.mkdir()
    canonical_config = _write(
        canonical_dir / "hl_mem.toml",
        (
            '[database]\npath = "data/memory.db"\n'
            '[usage]\nprice_book_path = "pricing/provider.json"\n'
            '[recall]\nquery_expansion_mode = "off"\n'
        ),
    )
    linked_config = tmp_path / "hl_mem.toml"
    linked_config.symlink_to(canonical_config)
    monkeypatch.chdir(source_dir)

    settings = _load_structural_settings(linked_config, tmp_path / ".env", environ={})

    assert settings.database_path == str((canonical_dir / "data" / "memory.db").resolve())
    assert settings.usage_price_book_path == str((canonical_dir / "pricing" / "provider.json").resolve())


@pytest.mark.parametrize(
    ("platform", "foreign_path", "expected"),
    [
        ("linux", "D:/hl_mem/prices.json", r"usage\.price_book_path.*Windows absolute path.*POSIX"),
        ("win32", "/opt/hl_mem/prices.json", r"usage\.price_book_path.*POSIX absolute path.*Windows"),
    ],
)
def test_price_book_rejects_foreign_absolute_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    foreign_path: str,
    expected: str,
) -> None:
    config_path = _write(
        tmp_path / "hl_mem.toml",
        f'[usage]\nprice_book_path = "{foreign_path}"\n[recall]\nquery_expansion_mode = "off"\n',
    )
    monkeypatch.setattr(sys, "platform", platform)

    with pytest.raises(ConfigurationError, match=expected):
        _load_structural_settings(config_path, tmp_path / ".env", environ={})


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
        _load_structural_settings(config_path, tmp_path / ".env", environ={})


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
        _load_structural_settings(config_path, tmp_path / ".env", environ={})


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

    settings = _load_structural_settings(config_path, tmp_path / ".env", environ={})

    assert settings.database_path == expected


def test_placeholder_error_is_redacted(tmp_path: Path) -> None:
    config_path = _write(
        tmp_path / "enabled.toml",
        """
[llm]
provider = "openai_compatible"
base_url = "https://llm.example.test/v1"
model = "quality-llm"
[extraction]
mode = "llm"
[embedding]
mode = "real"
base_url = "https://embedding.example.test/v1"
model = "quality-embedding"
dim = 2048
api_mode = "compatible"
[recall]
query_expansion_mode = "off"
""",
    )
    env_path = _write(tmp_path / ".env", "LLM_API_KEY=sk-xxx\n")

    with pytest.raises(ConfigurationError) as caught:
        _load_runtime_settings(config_path, env_path, environ={"EMBEDDING_API_KEY": "embedding-secret"})

    message = str(caught.value)
    assert str(config_path) in message
    assert "LLM_API_KEY" in message
    assert "sk-xxx" not in message


def test_missing_enabled_component_keys_explain_env_and_toml_recovery(tmp_path: Path) -> None:
    config_path = _write(
        tmp_path / "enabled.toml",
        """
[extraction]
mode = "llm"
[llm]
provider = "openai_compatible"
base_url = "https://llm.example.test/v1"
model = "quality-llm"
[embedding]
mode = "real"
base_url = "https://embedding.example.test/v1"
model = "quality-embedding"
dim = 2048
api_mode = "compatible"
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
        _load_runtime_settings(config_path, tmp_path / ".env", environ={})

    message = str(caught.value)
    for secret_name in ("LLM_API_KEY", "EMBEDDING_API_KEY", "RERANKER_API_KEY", "IMAGE_API_KEY"):
        assert secret_name in message
    assert ".env" in message
    assert "configure a production LLM service" in message
    assert "configure a production Embedding service" in message
    assert "reranker.mode='off'" in message
    assert "image_describer.mode='off'" in message


def test_every_settings_field_declares_exactly_one_source() -> None:
    for settings_field in fields(Settings):
        assert set(settings_field.metadata) in (
            {"toml"},
            {"secret_env"},
            {"plugin_namespace"},
            {"schema_version"},
        )


def test_provenance_mode_defaults_to_enforce_and_loads_observe(tmp_path: Path) -> None:
    default = Settings()
    configured = _load_structural_settings(
        _write(tmp_path / "provenance.toml", '[provenance]\nmode = "observe"\n'),
        environ={},
    )

    assert default.provenance_mode == "enforce"
    assert configured.provenance_mode == "observe"
    with pytest.raises(ConfigurationError, match="provenance.mode"):
        Settings(provenance_mode="invalid").validate()
