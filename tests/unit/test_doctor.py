"""doctor 诊断命令的单元测试。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

import hl_mem.cli as cli_module
import hl_mem.doctor as doctor_module
from hl_mem.compatibility import (
    CONTEXT_PACKET_SCHEMA_MAJOR,
    CONTEXT_PACKET_SCHEMA_MINOR,
    DAEMON_CONTRACT_MAJOR,
    HERMES_PLUGIN_CONTRACT_MAJOR,
)
from hl_mem.doctor import (
    CheckResult,
    CheckStatus,
    DaemonProbe,
    _check_daemon_compatibility,
    _check_hermes,
    _check_plugin_compatibility,
    _check_provider_plugins,
    _check_usage_ledger,
    _check_wire_compatibility,
    count_code_migrations,
    probe_model_components,
    run_doctor,
)
from hl_mem.errors import ConfigurationError
from hl_mem.observability.usage import UsageAmount, UsageGovernor, UsageIdentity, UsageLimits
from hl_mem.plugins.contracts import ProviderCapability
from hl_mem.settings import Settings, is_placeholder_secret
from hl_mem.storage.backup import backup_database
from hl_mem.storage.database import Database

HERMES_PLUGIN_FILES = ("__init__.py", "plugin.yaml", "contract.json")


def _production_config(path: Path, database_name: str = "memory.db") -> Path:
    path.write_text(
        f"""schema_version = 1
[database]
path = "{database_name}"
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
""",
        encoding="utf-8",
    )
    return path


def _disable_network_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor_module,
        "probe_model_components",
        lambda _settings, **_kwargs: [
            CheckResult(CheckStatus.OK, "LLM API", "verified"),
            CheckResult(CheckStatus.OK, "Embedding API", "verified"),
        ],
    )
    monkeypatch.setattr(doctor_module, "_probe_daemon", lambda _settings: DaemonProbe(None, "offline"))
    monkeypatch.setattr(
        doctor_module,
        "_check_hermes",
        lambda _settings: CheckResult(CheckStatus.WARN, "Hermes 插件", "not installed"),
    )
    monkeypatch.setattr(
        doctor_module,
        "_check_plugin_compatibility",
        lambda _settings: CheckResult(CheckStatus.WARN, "Hermes 插件兼容性", "not installed"),
    )


def _copy_packaged_plugin(target: Path) -> None:
    source = Path(doctor_module.__file__).resolve().parent / "adapters" / "hermes" / "plugin"
    target.mkdir(parents=True)
    for name in HERMES_PLUGIN_FILES:
        (target / name).write_bytes((source / name).read_bytes())


def test_doctor_runs_without_crashing(tmp_path: Path, monkeypatch) -> None:
    """离线配置下 doctor 应完整返回所有检查结果。"""
    database_path = tmp_path / "doctor.db"
    database = Database(database_path)
    database.open()
    database.close()
    config_path = tmp_path / "hl_mem.toml"
    config_path.write_text(
        'schema_version = 1\n[extraction]\nmode = "fake"\n[embedding]\nmode = "fake"\n'
        '[recall]\nquery_expansion_mode = "off"\n',
        encoding="utf-8",
    )
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "hl_mem.doctor._check_port",
        lambda: CheckResult(CheckStatus.WARN, "服务端口", "测试跳过"),
    )
    monkeypatch.setattr(
        "hl_mem.doctor._probe_daemon",
        lambda settings: DaemonProbe(None, "测试离线"),
    )
    results = run_doctor(
        database_path=database_path,
        config_path=config_path,
        env_path=env_path,
        environ={},
    )
    assert len(results) == 18


def test_provider_doctor_reports_resolution_and_trust_without_disabled_imports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = replace(Settings.for_test(), database_path=str(tmp_path / "memory.db"))
    calls = 0
    original = doctor_module.components.make_provider_registry

    def registry(_settings):
        nonlocal calls
        calls += 1
        return original(Settings.for_test(), entry_points=())

    monkeypatch.setattr(doctor_module.components, "make_provider_registry", registry)
    results = _check_provider_plugins(settings)

    assert calls == 1
    assert [(item.code, item.status) for item in results] == [
        ("provider_plugins", CheckStatus.OK),
        ("provider_trust", CheckStatus.OK),
    ]
    assert "dashscope" in results[0].detail


def test_usage_ledger_doctor_is_read_only_and_previews_expired_recovery(tmp_path: Path) -> None:
    database_path = tmp_path / "memory.db"
    ledger_path = tmp_path / "memory.budget.db"
    missing = _check_usage_ledger(replace(Settings.for_test(), database_path=str(database_path)))
    assert (missing.status, ledger_path.exists()) == (CheckStatus.WARN, False)

    def old_now() -> datetime:
        return datetime(2000, 1, 1, tzinfo=timezone.utc)

    governor = UsageGovernor(ledger_path, UsageLimits(), lease_seconds=1, now=old_now)
    identity = UsageIdentity(ProviderCapability.LLM, "test", "hl-mem.builtin", "dashscope", "model")
    governor.reserve(identity, UsageAmount(requests=1))
    sent = governor.reserve(identity, UsageAmount(requests=1))
    governor.mark_attempt(sent.id)

    result = _check_usage_ledger(replace(Settings.for_test(), database_path=str(database_path)))

    assert result.status is CheckStatus.WARN
    assert "expired_unsent=1" in result.detail
    assert "expired_ambiguous=1" in result.detail
    with sqlite3.connect(ledger_path) as connection:
        assert connection.execute("SELECT count(*) FROM usage_events").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM usage_reservations WHERE state='active'").fetchone()[0] == 2


def test_usage_price_book_doctor_validates_configured_book_without_disclosing_provenance(tmp_path: Path) -> None:
    check = getattr(doctor_module, "_check_usage_price_book", None)
    assert callable(check), "doctor does not validate configured usage price books"
    source_url = "https://pricing.example.test/private-source"
    price_path = tmp_path / "private-pricing.json"
    price_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "currency": "CNY",
                "effective_date": "2026-09-01",
                "source_urls": [source_url],
                "rules": [
                    {
                        "capability": "llm",
                        "model": "qwen",
                        "rates_microunits": {
                            "request": 0,
                            "million_input_tokens": 0,
                            "million_output_tokens": 0,
                            "embedding_item": 0,
                            "rerank_document": 0,
                            "image": 0,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    configured = replace(Settings.for_test(), usage_price_book_path=str(price_path))

    valid = check(configured)
    missing = check(replace(configured, usage_price_book_path=str(tmp_path / "missing.json")))

    assert valid is not None
    assert (valid.code, valid.status) == ("usage_price_book", CheckStatus.OK)
    assert "configured=true" in valid.detail
    assert "fingerprint=" in valid.detail
    assert missing is not None
    assert (missing.code, missing.status) == ("usage_price_book", CheckStatus.FAIL)
    rendered = repr((valid.to_dict(), missing.to_dict()))
    assert str(tmp_path) not in rendered
    assert source_url not in rendered


def test_usage_price_book_doctor_is_absent_when_not_configured() -> None:
    check = getattr(doctor_module, "_check_usage_price_book", None)
    assert callable(check), "doctor does not validate configured usage price books"
    assert check(Settings.for_test()) is None


def test_invalid_config_is_a_structured_failure(tmp_path: Path) -> None:
    path = tmp_path / "hl_mem.toml"
    path.write_text("schema_version = 2\n", encoding="utf-8")

    [result] = run_doctor(config_path=path, environ={})

    assert (result.code, result.status) == ("config", CheckStatus.FAIL)
    assert set(result.to_dict()) == {"code", "status", "name", "detail"}


def test_doctor_json_output_is_machine_readable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _production_config(tmp_path / "hl_mem.toml")
    env = tmp_path / ".env"
    env.write_text("LLM_API_KEY=test-llm\nEMBEDDING_API_KEY=test-embedding\n", encoding="utf-8")
    database = Database(tmp_path / "memory.db")
    database.open()
    database.close()
    _disable_network_probes(monkeypatch)

    exit_code = doctor_module.main(["--config", str(config), "--env-file", str(env), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["summary"]["failures"] == 0
    assert all(set(item) == {"code", "status", "name", "detail"} for item in payload["checks"])


def test_management_cli_forwards_structured_doctor_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def fake_doctor_main(arguments: list[str]) -> int:
        captured.extend(arguments)
        return 0

    monkeypatch.setattr(doctor_module, "main", fake_doctor_main)
    with pytest.raises(SystemExit, match="0"):
        cli_module.main(
            [
                "doctor",
                "--json",
                "--backup",
                str(tmp_path / "backup.db"),
                "--manifest",
                str(tmp_path / "manifest.json"),
            ]
        )

    assert captured == [
        "--backup",
        str(tmp_path / "backup.db"),
        "--manifest",
        str(tmp_path / "manifest.json"),
        "--json",
    ]


def test_python_311_is_reported_as_unsupported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _production_config(tmp_path / "hl_mem.toml")
    monkeypatch.setattr(doctor_module.sys, "version_info", (3, 11, 9))
    _disable_network_probes(monkeypatch)

    results = run_doctor(config_path=config, environ={"LLM_API_KEY": "a", "EMBEDDING_API_KEY": "b"})

    python = next(item for item in results if item.code == "python")
    assert python.status is CheckStatus.FAIL


def test_doctor_recovery_evidence_is_optional_but_verifiable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _production_config(tmp_path / "hl_mem.toml")
    database_path = tmp_path / "memory.db"
    database = Database(database_path)
    database.open()
    database.close()
    backup = tmp_path / "recovery.db"
    manifest = backup_database(database_path, backup)
    _disable_network_probes(monkeypatch)
    environment = {"LLM_API_KEY": "a", "EMBEDDING_API_KEY": "b"}

    without = run_doctor(config_path=config, environ=environment)
    with_evidence = run_doctor(
        config_path=config,
        environ=environment,
        backup_path=backup,
        manifest_path=manifest,
    )

    recovery_without = [item for item in without if item.code == "recovery"]
    recovery_with = [item for item in with_evidence if item.code == "recovery"]
    assert len(recovery_without) == 1
    assert recovery_without[0].status is CheckStatus.WARN
    assert len(recovery_with) == 1
    assert recovery_with[0].status is CheckStatus.OK


def test_doctor_accepts_matching_daemon_and_wire_contracts() -> None:
    probe = DaemonProbe(
        {
            "version": "0.29.0",
            "compatibility": {
                "daemon_contract_major": DAEMON_CONTRACT_MAJOR,
                "required_plugin_contract_major": HERMES_PLUGIN_CONTRACT_MAJOR,
                "context_packet": {
                    "schema_major": CONTEXT_PACKET_SCHEMA_MAJOR,
                    "schema_minor": CONTEXT_PACKET_SCHEMA_MINOR,
                },
            },
        },
        None,
    )

    assert _check_daemon_compatibility(probe).status is CheckStatus.OK
    assert _check_wire_compatibility(probe).status is CheckStatus.OK


def test_doctor_rejects_daemon_or_wire_major_mismatch() -> None:
    probe = DaemonProbe(
        {
            "version": "0.30.0",
            "compatibility": {
                "daemon_contract_major": DAEMON_CONTRACT_MAJOR + 1,
                "required_plugin_contract_major": HERMES_PLUGIN_CONTRACT_MAJOR,
                "context_packet": {
                    "schema_major": CONTEXT_PACKET_SCHEMA_MAJOR + 1,
                    "schema_minor": 0,
                },
            },
        },
        None,
    )

    assert _check_daemon_compatibility(probe).status is CheckStatus.FAIL
    assert _check_wire_compatibility(probe).status is CheckStatus.FAIL


def test_doctor_rejects_daemon_required_plugin_major_mismatch() -> None:
    probe = DaemonProbe(
        {
            "version": "0.29.0",
            "compatibility": {
                "daemon_contract_major": DAEMON_CONTRACT_MAJOR,
                "required_plugin_contract_major": HERMES_PLUGIN_CONTRACT_MAJOR + 1,
                "context_packet": {
                    "schema_major": CONTEXT_PACKET_SCHEMA_MAJOR,
                    "schema_minor": CONTEXT_PACKET_SCHEMA_MINOR,
                },
            },
        },
        None,
    )

    result = _check_daemon_compatibility(probe)

    assert result.status is CheckStatus.FAIL
    assert "required_plugin_contract_major" in result.detail


def test_doctor_warns_when_daemon_contract_cannot_be_observed() -> None:
    probe = DaemonProbe(None, "connection refused")

    assert _check_daemon_compatibility(probe).status is CheckStatus.WARN
    assert _check_wire_compatibility(probe).status is CheckStatus.WARN


def test_doctor_accepts_matching_installed_plugin_contract(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugins" / "hl_mem"
    _copy_packaged_plugin(plugin_dir)

    result = _check_plugin_compatibility(Settings(hermes_home=str(tmp_path)))

    assert result.status is CheckStatus.OK


def test_doctor_rejects_incompatible_installed_plugin_contract(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugins" / "hl_mem"
    _copy_packaged_plugin(plugin_dir)
    contract_path = plugin_dir / "contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["context_packet_schema_major"] = CONTEXT_PACKET_SCHEMA_MAJOR + 1
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    result = _check_plugin_compatibility(Settings(hermes_home=str(tmp_path)))

    assert result.status is CheckStatus.FAIL
    assert "context_packet_schema_major" in result.detail


def test_placeholder_secret_detection() -> None:
    """空值和常见模板值应被识别，真实格式密钥不应误报。"""
    for value in (None, "", "xxx", "your-key", "<API_KEY>", "sk-xxx", "sk-e72xxx"):
        assert is_placeholder_secret(value)
    assert not is_placeholder_secret("sk-live-abcdef123456")


def test_model_probe_includes_only_enabled_model_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor_module,
        "_check_llm",
        lambda _settings, runtime=None: CheckResult(CheckStatus.OK, "LLM API", "ok"),
    )
    monkeypatch.setattr(
        doctor_module,
        "_check_embedding",
        lambda _settings, runtime=None: CheckResult(CheckStatus.OK, "Embedding API", "ok"),
    )
    monkeypatch.setattr(
        doctor_module,
        "_check_reranker",
        lambda _settings, runtime=None: CheckResult(CheckStatus.OK, "Reranker API", "ok"),
    )

    without_reranker = probe_model_components(Settings.for_test())
    with_reranker = probe_model_components(Settings(reranker_mode="real"))

    assert [item.name for item in without_reranker] == ["LLM API", "Embedding API"]
    assert [item.name for item in with_reranker] == ["LLM API", "Embedding API", "Reranker API"]


def test_run_doctor_loads_one_price_book_and_shares_one_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hl_mem.observability.pricing import UsagePriceBook

    config = _production_config(tmp_path / "config.toml")
    price_path = tmp_path / "prices.json"
    price_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "currency": "CNY",
                "effective_date": "2026-08-31",
                "rules": [
                    {
                        "capability": "llm",
                        "provider": "openai_compatible",
                        "model": "quality-llm",
                        "rates_microunits": {
                            "request": 1,
                            "million_input_tokens": 0,
                            "million_output_tokens": 0,
                            "embedding_item": 0,
                            "rerank_document": 0,
                            "image": 0,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with config.open("a", encoding="utf-8") as stream:
        stream.write(f'\n[usage]\nprice_book_path = "{price_path.as_posix()}"\n')

    original_load = UsagePriceBook.load.__func__
    load_count = 0

    def counting_load(cls: type[UsagePriceBook], path: Path) -> UsagePriceBook:
        nonlocal load_count
        load_count += 1
        return original_load(cls, path)

    runtimes: list[object] = []
    runtime_fingerprints: list[object] = []

    def checked(_settings: Settings, runtime=None) -> CheckResult:
        assert runtime is not None
        runtimes.append(runtime)
        runtime_fingerprints.append(runtime.usage_snapshot()["price_book_fingerprint"])
        return CheckResult(CheckStatus.OK, "model", "ok")

    monkeypatch.setattr(UsagePriceBook, "load", classmethod(counting_load))
    monkeypatch.setattr(doctor_module, "_check_llm", checked)
    monkeypatch.setattr(doctor_module, "_check_embedding", checked)
    monkeypatch.setattr(doctor_module, "_probe_daemon", lambda _settings: DaemonProbe(None, "offline"))

    results = run_doctor(
        config_path=config,
        environ={"LLM_API_KEY": "live-llm", "EMBEDDING_API_KEY": "live-embedding"},
    )

    price_check = next(result for result in results if result.code == "usage_price_book")
    assert load_count == 1
    assert len(runtimes) == 2 and runtimes[0] is runtimes[1]
    assert len(set(runtime_fingerprints)) == 1
    assert runtime_fingerprints[0] in price_check.detail


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
        settings.validate_runtime()

    with pytest.raises(ConfigurationError, match="LLM_API_KEY"):
        Settings(extractor_mode="real", llm_api_key="your-key").validate_runtime()


def test_hermes_check_uses_user_plugin_root_even_with_agent_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """源码 checkout 存在时仍应检查 HERMES_HOME 根下的用户插件。"""
    expected = tmp_path / "plugins" / "hl_mem"
    _copy_packaged_plugin(expected)
    agent_checkout = tmp_path / "hermes-agent"
    agent_checkout.mkdir()
    (agent_checkout / "pyproject.toml").touch()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    result = _check_hermes(Settings())

    assert result == CheckResult(CheckStatus.OK, "Hermes 插件", f"路径正确且无漂移：{expected}")


def test_hermes_check_reports_legacy_plugin_path(tmp_path: Path) -> None:
    """插件位于 legacy 目录时应同时指出实际路径与期望路径。"""
    actual = tmp_path / "plugins" / "memory" / "hl_mem"
    expected = tmp_path / "plugins" / "hl_mem"
    _copy_packaged_plugin(actual)

    result = _check_hermes(Settings(hermes_home=str(tmp_path)))

    assert result.status is CheckStatus.FAIL
    assert str(actual) in result.detail
    assert str(expected) in result.detail


def test_hermes_check_reports_plugin_under_unmarked_agent_subdirectory(tmp_path: Path) -> None:
    """旧猜测逻辑装入无标志 hermes-agent 子目录时应报告实际与期望路径。"""
    actual = tmp_path / "hermes-agent" / "plugins" / "hl_mem"
    expected = tmp_path / "plugins" / "hl_mem"
    _copy_packaged_plugin(actual)

    result = _check_hermes(Settings(hermes_home=str(tmp_path)))

    assert result.status is CheckStatus.FAIL
    assert str(actual) in result.detail
    assert str(expected) in result.detail


def test_hermes_check_prefers_installed_legacy_copy_over_empty_expected_directory(tmp_path: Path) -> None:
    """空期望目录不能遮蔽 legacy 路径中的完整插件副本。"""
    expected = tmp_path / "plugins" / "hl_mem"
    expected.mkdir(parents=True)
    actual = tmp_path / "plugins" / "memory" / "hl_mem"
    _copy_packaged_plugin(actual)

    result = _check_hermes(Settings(hermes_home=str(tmp_path)))

    assert result.status is CheckStatus.FAIL
    assert str(actual) in result.detail
    assert str(expected) in result.detail


def test_hermes_check_ignores_empty_legacy_directory(tmp_path: Path) -> None:
    """空 legacy 目录不是已安装插件，不能被报告为实际路径。"""
    expected = tmp_path / "plugins" / "hl_mem"
    (tmp_path / "plugins" / "memory" / "hl_mem").mkdir(parents=True)

    result = _check_hermes(Settings(hermes_home=str(tmp_path)))

    assert result == CheckResult(CheckStatus.FAIL, "Hermes 插件", f"应安装到 {expected}")


def test_hermes_check_reports_plugin_drift(tmp_path: Path) -> None:
    """期望目录中的副本与包内文件不同应提示 upgrade。"""
    expected = tmp_path / "plugins" / "hl_mem"
    _copy_packaged_plugin(expected)
    (expected / "plugin.yaml").write_bytes(b"drifted")

    result = _check_hermes(Settings(hermes_home=str(tmp_path)))

    assert result == CheckResult(
        CheckStatus.FAIL,
        "Hermes 插件",
        "检测到插件副本漂移，运行 hl-mem hermes upgrade",
    )
