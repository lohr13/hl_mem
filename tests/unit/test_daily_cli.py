from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import hl_mem.cli as cli_module
import hl_mem.daily_cli as daily_cli
import hl_mem.server as server_module
from hl_mem.cli import main
from hl_mem.config.secrets import merge_secret_file
from hl_mem.config_loader import load_settings
from hl_mem.doctor import CheckResult, CheckStatus
from hl_mem.settings import Settings


def _use_http_handler(monkeypatch: pytest.MonkeyPatch, handler: httpx.MockTransport) -> None:
    monkeypatch.setattr(
        daily_cli,
        "_make_http_client",
        lambda base_url: httpx.Client(base_url=base_url, transport=handler),
        raising=False,
    )


def _wizard_answers(monkeypatch: pytest.MonkeyPatch, *, reranker: str = "n") -> tuple[str, str]:
    answers = iter(
        (
            "openai_compatible",
            "https://llm.example.test/v1",
            "quality-llm",
            "https://embedding.example.test/v1",
            "quality-embedding",
            "2048",
            "compatible",
            reranker,
        )
    )
    secrets = iter(("llm-secret-value", "embedding-secret-value"))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(daily_cli.getpass, "getpass", lambda _prompt: next(secrets))
    monkeypatch.setattr(
        daily_cli,
        "probe_model_components",
        lambda _settings: [
            CheckResult(CheckStatus.OK, "LLM API", "verified"),
            CheckResult(CheckStatus.OK, "Embedding API", "verified"),
        ],
    )
    return "llm-secret-value", "embedding-secret-value"


def test_init_wizard_writes_verified_provider_neutral_configuration(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "hl_mem.toml"
    env_path = tmp_path / ".env"
    llm_secret, embedding_secret = _wizard_answers(monkeypatch)

    main(["init", "--config", str(config_path), "--env-file", str(env_path)])

    settings = load_settings(config_path, env_path, environ={})
    assert settings.extractor_mode == "llm"
    assert settings.embedder_mode == "real"
    assert settings.reranker_mode == "off"
    assert settings.image_describer_mode == "off"
    assert settings.query_expansion_mode == "off"
    assert settings.resurrection_mode == "off"
    assert settings.relation_discovery_mode == "off"
    document = config_path.read_text(encoding="utf-8")
    assert document.startswith("schema_version = 1\n")
    assert llm_secret not in document
    assert embedding_secret not in document
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert str(config_path) in output
    assert llm_secret not in output
    assert embedding_secret not in output


def test_init_rejects_removed_offline_profile(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["init", "--offline"])

    assert error.value.code == 2
    assert "unrecognized arguments: --offline" in capsys.readouterr().err


def test_init_requires_force_before_overwriting_existing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "hl_mem.toml"
    config_path.write_text("keep-me", encoding="utf-8")

    with pytest.raises(SystemExit) as error:
        main(["init", "--config", str(config_path)])

    assert error.value.code == 2
    assert config_path.read_text(encoding="utf-8") == "keep-me"

    _wizard_answers(monkeypatch)
    main(["init", "--force", "--config", str(config_path)])
    assert config_path.read_text(encoding="utf-8").startswith("schema_version = 1\n")


def test_init_preserves_unrelated_env_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "hl_mem.toml"
    env_path = tmp_path / ".env"
    env_path.write_text("# user setting\nUNRELATED=value\n", encoding="utf-8")
    _wizard_answers(monkeypatch)

    main(["init", "--config", str(config_path), "--env-file", str(env_path)])

    secret_file = env_path.read_text(encoding="utf-8")
    assert "# user setting\nUNRELATED=value\n" in secret_file
    assert "LLM_API_KEY=llm-secret-value" in secret_file
    assert "EMBEDDING_API_KEY=embedding-secret-value" in secret_file


def test_init_probe_failure_changes_neither_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "hl_mem.toml"
    env_path = tmp_path / ".env"
    config_path.write_text("original-config", encoding="utf-8")
    env_path.write_text("ORIGINAL=secret-safe\n", encoding="utf-8")
    llm_secret, embedding_secret = _wizard_answers(monkeypatch)
    monkeypatch.setattr(
        daily_cli,
        "probe_model_components",
        lambda _settings: [CheckResult(CheckStatus.FAIL, "LLM API", f"failed using {llm_secret}")],
    )

    with pytest.raises(SystemExit) as error:
        main(
            [
                "init",
                "--force",
                "--config",
                str(config_path),
                "--env-file",
                str(env_path),
            ]
        )

    assert error.value.code == 1
    assert config_path.read_text(encoding="utf-8") == "original-config"
    assert env_path.read_text(encoding="utf-8") == "ORIGINAL=secret-safe\n"
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert llm_secret not in output
    assert embedding_secret not in output


def test_init_write_failure_restores_exact_secret_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "hl_mem.toml"
    env_path = tmp_path / ".env"
    original = b"# exact original\r\nUNRELATED=value\r\n"
    env_path.write_bytes(original)
    _wizard_answers(monkeypatch)
    monkeypatch.setattr(daily_cli, "_write_config_atomic", lambda *_args: (_ for _ in ()).throw(OSError("disk")))

    with pytest.raises(SystemExit) as error:
        main(["init", "--config", str(config_path), "--env-file", str(env_path)])

    assert error.value.code == 1
    assert env_path.read_bytes() == original
    assert not config_path.exists()


def test_secret_merge_requires_force_to_replace_supported_values(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("UNRELATED=keep\nLLM_API_KEY=old\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="--force"):
        merge_secret_file(env_path, {"LLM_API_KEY": "new"})
    assert env_path.read_text(encoding="utf-8") == "UNRELATED=keep\nLLM_API_KEY=old\n"

    merge_secret_file(env_path, {"LLM_API_KEY": "new"}, force=True)
    assert env_path.read_text(encoding="utf-8") == "UNRELATED=keep\nLLM_API_KEY=new\n"


def test_remember_posts_explicit_memory_and_prints_event_id(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/memories"
        assert json.loads(request.content) == {"text": "Alice 喜欢深色模式"}
        return httpx.Response(200, json={"id": "event-1", "created": True})

    _use_http_handler(monkeypatch, httpx.MockTransport(handle))

    main(["remember", "Alice 喜欢深色模式"])

    output = capsys.readouterr().out
    assert "已提交记忆" in output
    assert "event-1" in output
    assert "事件 ID" in output


def test_recall_prints_scores_and_evidence(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/recall"
        assert json.loads(request.content) == {"query": "Alice 喜欢什么", "limit": 3}
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "claim-1",
                        "text": "Alice 喜欢深色模式",
                        "score": 0.81234,
                        "evidence": [{"source_type": "event", "source_id": "event-1"}],
                    }
                ],
                "observations": [],
                "policies": [],
                "total": 1,
            },
        )

    _use_http_handler(monkeypatch, httpx.MockTransport(handle))

    main(["recall", "Alice 喜欢什么", "--limit", "3"])

    output = capsys.readouterr().out
    assert "Alice 喜欢深色模式" in output
    assert "claim-1" in output
    assert "0.8123" in output
    assert "event-1" in output


def test_list_requests_page_and_prints_memory_ids(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/memories"
        assert dict(request.url.params) == {
            "limit": "2",
            "offset": "2",
            "status": "active",
            "namespace": "default",
        }
        return httpx.Response(
            200,
            json={
                "memories": [
                    {
                        "id": "claim-3",
                        "text": "第三条记忆",
                        "status": "active",
                        "recorded_from": "2026-08-03T00:00:00+00:00",
                        "valid_from": None,
                        "canonical_slot": None,
                        "topic_tags": [],
                    }
                ],
                "total": 3,
                "limit": 2,
                "offset": 2,
            },
        )

    _use_http_handler(monkeypatch, httpx.MockTransport(handle))

    main(["list", "--limit", "2", "--offset", "2"])

    output = capsys.readouterr().out
    assert "claim-3" in output
    assert "第三条记忆" in output
    assert "3 条" in output


def test_forget_deletes_claim_and_prints_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/v1/memories/claim-1"
        return httpx.Response(200, json={"id": "claim-1", "forgotten": True})

    _use_http_handler(monkeypatch, httpx.MockTransport(handle))

    main(["forget", "claim-1"])

    assert "已撤回记忆 claim-1" in capsys.readouterr().out


def test_correct_posts_replacement_and_prints_new_claim_id(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/memories/claim-old/correct"
        assert json.loads(request.content) == {"corrected_text": "新配置"}
        return httpx.Response(
            200,
            json={
                "correction_event_id": "correction-1",
                "new_claim_id": "claim-new",
                "created": True,
            },
        )

    _use_http_handler(monkeypatch, httpx.MockTransport(handle))

    main(["correct", "claim-old", "--text", "新配置", "--url", "http://127.0.0.1:8300"])

    output = capsys.readouterr().out
    assert "已纠正记忆 claim-old" in output
    assert "claim-new" in output
    assert "correction-1" in output


def test_correct_help_lists_required_text_and_optional_url(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["correct", "--help"])

    assert error.value.code == 0
    help_text = capsys.readouterr().out
    assert "memory_id" in help_text
    assert "--text" in help_text
    assert "--url" in help_text


def test_daily_command_reports_how_to_start_server_when_unreachable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _use_http_handler(monkeypatch, httpx.MockTransport(handle))

    with pytest.raises(SystemExit) as error:
        main(["recall", "test"])

    assert error.value.code == 1
    assert "hl-mem server" in capsys.readouterr().err


def test_server_command_loads_selected_config_and_reuses_server_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "server.toml"
    calls: dict[str, object] = {}

    def fake_load(config: Path | None, env_file: Path | None) -> Settings:
        calls["load"] = (config, env_file)
        return Settings.for_test()

    def fake_run(settings: Settings, *, host: str, port: int) -> None:
        calls["run"] = (settings, host, port)

    monkeypatch.setattr(cli_module, "load_settings", fake_load)
    monkeypatch.setattr(server_module, "run_server", fake_run)

    main(["server", "--config", str(config_path), "--host", "0.0.0.0", "--port", "8300"])

    assert calls["load"] == (config_path, None)
    assert calls["run"] == (Settings.for_test(), "0.0.0.0", 8300)
