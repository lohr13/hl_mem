from __future__ import annotations

import hashlib
import json
import re
import shutil
import zipfile
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema import ValidationError as JsonSchemaValidationError

from benchmarks.provider import live_smoke as live_smoke_module
from benchmarks.provider.live_smoke import (
    LiveSmokeBudgetError,
    LiveSmokeLimits,
    LiveSmokeSafetyError,
    _LiveSmokeDependencies,
    _run_live_smoke,
    main,
    run_live_smoke,
)
from hl_mem.errors import ProviderCallError
from hl_mem.llm.types import LLMCapabilities, LLMResponse
from hl_mem.observability.pricing import UsagePriceBook
from hl_mem.observability.usage import UsageAmount, UsageGovernor, UsageIdentity, UsageLimits
from hl_mem.plugins.contracts import (
    EmbeddingInvocation,
    EmbeddingResult,
    LLMInvocation,
    ProviderCapability,
    ProviderCapabilitySpec,
    ProviderEndpoint,
    ProviderKey,
    ProviderManifest,
    ProviderPlugin,
    ProviderRequest,
    ProviderResponse,
    ProviderStability,
    RerankInvocation,
    RerankResult,
)
from hl_mem.plugins.proxies import GovernedProviderCall
from hl_mem.plugins.transport import ProviderTransport
from scripts.check_wheel_contents import check_wheel

LIMITS = LiveSmokeLimits()
FIXTURE_PATH = Path(__file__).parents[2] / "benchmarks" / "provider" / "fixture.json"
RESULT_SCHEMA_PATH = Path(__file__).parents[2] / "benchmarks" / "provider" / "result_schema.json"


class _RecordingLLMAdapter:
    capabilities = LLMCapabilities(json_object=True, json_schema_strict=True)

    def build_request(self, endpoint: ProviderEndpoint, invocation: LLMInvocation) -> ProviderRequest:
        return ProviderRequest(
            "POST",
            f"{endpoint.base_url.rstrip('/')}/llm",
            {"Authorization": f"Bearer {endpoint.api_key}"},
            {"messages": [item.content for item in invocation.request.messages]},
            endpoint.timeout_seconds,
        )

    def parse_response(self, response: ProviderResponse) -> LLMResponse:
        return LLMResponse(
            content=str(response.json_body["content"]),
            finish_reason="stop",
            usage_total_tokens=96,
            raw_request_id=response.request_id,
            input_tokens=64,
            output_tokens=32,
        )

    def is_structured_mode_unsupported(self, _error: Exception) -> bool:
        return False


class _RecordingEmbeddingAdapter:
    def build_request(self, endpoint: ProviderEndpoint, invocation: EmbeddingInvocation) -> ProviderRequest:
        return ProviderRequest(
            "POST",
            f"{endpoint.base_url.rstrip('/')}/embedding",
            {"Authorization": f"Bearer {endpoint.api_key}"},
            {"texts": list(invocation.texts), "dimensions": invocation.dimensions},
            endpoint.timeout_seconds,
        )

    def parse_response(self, response: ProviderResponse) -> EmbeddingResult:
        input_tokens = response.json_body.get("input_tokens")
        return EmbeddingResult(
            tuple(tuple(float(value) for value in vector) for vector in response.json_body["vectors"]),
            input_tokens=None if input_tokens is None else int(input_tokens),
        )


class _RecordingRerankerAdapter:
    def build_request(self, endpoint: ProviderEndpoint, invocation: RerankInvocation) -> ProviderRequest:
        return ProviderRequest(
            "POST",
            f"{endpoint.base_url.rstrip('/')}/reranker",
            {"Authorization": f"Bearer {endpoint.api_key}"},
            {"query": invocation.query, "documents": list(invocation.documents), "top_n": invocation.top_n},
            endpoint.timeout_seconds,
        )

    def parse_response(self, response: ProviderResponse) -> RerankResult:
        return RerankResult(
            tuple((int(index), float(score)) for index, score in response.json_body["results"]),
            input_tokens=int(response.json_body["input_tokens"]),
            output_tokens=int(response.json_body["output_tokens"]),
        )


class _RecordingEntryPoint:
    name = "recording.plugin"
    value = "tests.recording:plugin"
    dist = SimpleNamespace(name="recording-provider")

    def __init__(self) -> None:
        specs = tuple(
            ProviderCapabilitySpec("recording", capability, ProviderStability.STABLE)
            for capability in (
                ProviderCapability.LLM,
                ProviderCapability.EMBEDDING,
                ProviderCapability.RERANKER,
            )
        )
        adapters: dict[ProviderCapability, object] = {
            ProviderCapability.LLM: _RecordingLLMAdapter(),
            ProviderCapability.EMBEDDING: _RecordingEmbeddingAdapter(),
            ProviderCapability.RERANKER: _RecordingRerankerAdapter(),
        }
        self.plugin = ProviderPlugin(
            ProviderManifest(
                id=self.name,
                version="1.0.0",
                api_version=1,
                requires_hl_mem=">=0.36,<2",
                capabilities=specs,
                config_schema={"type": "object", "properties": {}, "additionalProperties": False},
            ),
            {
                ProviderKey(item.capability, item.name): (lambda _context, value=adapters[item.capability]: value)
                for item in specs
            },
        )

    def load(self):  # type: ignore[no-untyped-def]
        return lambda: self.plugin


class _RecordingTransport:
    def __init__(
        self,
        *,
        unknown_embedding_usage: bool = False,
        fail_reranker: bool = False,
        malformed_llm: bool = False,
        invalid_embedding_count: bool = False,
        llm_failures_before_success: int = 0,
    ) -> None:
        self.requests: list[dict[str, Any]] = []
        self.unknown_embedding_usage = unknown_embedding_usage
        self.fail_reranker = fail_reranker
        self.malformed_llm = malformed_llm
        self.invalid_embedding_count = invalid_embedding_count
        self.llm_failures_before_success = llm_failures_before_success
        self.llm_attempts = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append({"url": str(request.url), "authorization": request.headers.get("Authorization")})
        if request.url.path.endswith("/llm"):
            self.llm_attempts += 1
            if self.llm_attempts <= self.llm_failures_before_success:
                return httpx.Response(503, request=request, json={"error": {"code": "controlled_unavailable"}})
            fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
            return httpx.Response(
                200,
                request=request,
                json={
                    "content": (
                        "not-json"
                        if self.malformed_llm
                        else json.dumps(fixture["recording_extraction"], ensure_ascii=False)
                    )
                },
                headers={"x-request-id": "recording-llm"},
            )
        if request.url.path.endswith("/embedding"):
            vectors = []
            for text in payload["texts"]:
                digest = hashlib.sha256(text.encode("utf-8")).digest()
                vectors.append([1.0, digest[0] / 255.0, digest[1] / 255.0, digest[2] / 255.0])
            return httpx.Response(
                200,
                request=request,
                json={
                    "vectors": vectors[:-1] if self.invalid_embedding_count else vectors,
                    "input_tokens": None if self.unknown_embedding_usage else len(payload["texts"]) * 4,
                },
            )
        if self.fail_reranker:
            return httpx.Response(503, request=request, json={"error": {"code": "controlled_unavailable"}})
        documents = payload["documents"]
        return httpx.Response(
            200,
            request=request,
            json={
                "results": [[index, 1.0 - index * 0.1] for index in range(len(documents))],
                "input_tokens": len(documents) * 4,
                "output_tokens": len(documents),
            },
        )


def _write_price_book(
    tmp_path: Path,
    *,
    omit: str | None = None,
    request_rate: int = 1000,
    embedding_token_rate: int = 0,
    source_url: str = "https://pricing.example.test/provider",
) -> Path:
    rules = []
    for capability, model in (
        ("llm", "recording-llm"),
        ("embedding", "recording-embedding"),
        ("reranker", "recording-reranker"),
    ):
        if capability == omit:
            continue
        rules.append(
            {
                "capability": capability,
                "provider": "recording",
                "model": model,
                "rates_microunits": {
                    "request": request_rate,
                    "million_input_tokens": embedding_token_rate if capability == "embedding" else 0,
                    "million_output_tokens": 0,
                    "embedding_item": 100 if capability == "embedding" else 0,
                    "rerank_document": 10 if capability == "reranker" else 0,
                    "image": 0,
                },
            }
        )
    path = tmp_path / "prices.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "currency": "CNY",
                "effective_date": "2026-08-31",
                "source_urls": [source_url],
                "rules": rules,
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_env(path: Path, *, prefix: str = "explicit") -> Path:
    path.write_text(
        "\n".join(
            (
                f"LLM_API_KEY={prefix}-llm-secret",
                f"EMBEDDING_API_KEY={prefix}-embedding-secret",
                f"RERANKER_API_KEY={prefix}-reranker-secret",
                "",
            )
        ),
        encoding="utf-8",
    )
    return path


def _recording_dependencies(tmp_path: Path, recorder: _RecordingTransport, clients: list[httpx.Client]):
    def client_factory() -> httpx.Client:
        client = httpx.Client(transport=httpx.MockTransport(recorder))
        clients.append(client)
        return client

    return _LiveSmokeDependencies(
        entry_points=(_RecordingEntryPoint(),),
        client_factory=client_factory,
        temp_parent=tmp_path,
    )


def _run_with_recording_providers(
    tmp_path: Path,
    *,
    limits: LiveSmokeLimits = LIMITS,
    omit_price_rule: str | None = None,
    request_rate: int = 1000,
) -> tuple[dict[str, object], _RecordingTransport, list[httpx.Client], Path, Path]:
    config = _write_config(tmp_path)
    price_book = _write_price_book(tmp_path, omit=omit_price_rule, request_rate=request_rate)
    env_file = _write_env(tmp_path / "smoke.env", prefix="recording")
    recorder = _RecordingTransport()
    clients: list[httpx.Client] = []
    result = _run_live_smoke(
        config,
        tmp_path / "result.json",
        env_file=env_file,
        price_book=price_book,
        limits=limits,
        dependencies=_recording_dependencies(tmp_path, recorder, clients),
    )
    return result, recorder, clients, config, price_book


def _write_config(
    tmp_path: Path,
    *,
    database: str = "smoke.db",
    body: str = "",
    llm_max_attempts: int = 1,
) -> Path:
    path = tmp_path / "smoke.toml"
    encoded_database = json.dumps(database)
    path.write_text(
        f"""schema_version = 1

[database]
path = {encoded_database}

[llm]
provider = "recording"
base_url = "https://provider.invalid/v1"
model = "recording-llm"
max_attempts = {llm_max_attempts}

[extraction]
mode = "llm"

[embedding]
mode = "real"
provider = "recording"
base_url = "https://provider.invalid/v1"
model = "recording-embedding"
dim = 4
api_mode = "compatible"
max_attempts = 1

[reranker]
mode = "real"
provider = "recording"
base_url = "https://provider.invalid/v1"
model = "recording-reranker"

[recall]
query_expansion_mode = "off"

[usage]
price_book_path = "prices.json"

[plugins]
enabled = ["recording.plugin"]
{body}
""",
        encoding="utf-8",
    )
    return path


def test_live_smoke_limits_are_hard_capped() -> None:
    assert LIMITS == LiveSmokeLimits(10, 30, 100, 20_000_000)
    with pytest.raises(ValueError, match="hard cap"):
        LiveSmokeLimits(llm_requests=11)


@pytest.mark.parametrize(
    "database",
    (
        "D:/production/hl_mem.db",
        r"\\server\share\hl_mem.db",
        "../hl_mem.db",
        "nested/hl_mem.db",
    ),
)
def test_live_smoke_refuses_database_path_not_owned_by_temporary_root(
    tmp_path: Path,
    database: str,
) -> None:
    config = _write_config(tmp_path, database=database)
    env_file = _write_env(tmp_path / "smoke.env", prefix="recording")
    price_book = _write_price_book(tmp_path)
    with pytest.raises(LiveSmokeSafetyError, match="temporary root"):
        run_live_smoke(
            config,
            tmp_path / "result.json",
            env_file=env_file,
            price_book=price_book,
            limits=LIMITS,
        )


@pytest.mark.parametrize(
    "section, replacement",
    (
        ("extraction", '[extraction]\nmode = "fake"'),
        ("embedding", '[embedding]\nmode = "fake"'),
        ("reranker", '[reranker]\nmode = "fake"'),
    ),
)
def test_live_smoke_refuses_fake_components(
    tmp_path: Path,
    section: str,
    replacement: str,
) -> None:
    config = _write_config(tmp_path)
    text = config.read_text(encoding="utf-8")
    start = text.index(f"[{section}]")
    next_section = text.find("\n[", start + 1)
    config.write_text(text[:start] + replacement + text[next_section:], encoding="utf-8")
    env_file = _write_env(tmp_path / "smoke.env", prefix="recording")
    price_book = _write_price_book(tmp_path)

    with pytest.raises(LiveSmokeSafetyError, match="Fake"):
        run_live_smoke(
            config,
            tmp_path / "result.json",
            env_file=env_file,
            price_book=price_book,
            limits=LIMITS,
        )


def test_live_smoke_requires_a_price_book(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    env_file = _write_env(tmp_path / "smoke.env", prefix="recording")

    with pytest.raises(TypeError, match="price_book"):
        run_live_smoke(config, tmp_path / "result.json", env_file=env_file, limits=LIMITS)


def test_cli_requires_explicit_env_file_and_price_book(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as captured:
        main(["--config", "smoke.toml", "--output", "result.json"])

    assert captured.value.code == 2
    stderr = capsys.readouterr().err
    assert "--env-file" in stderr
    assert "--price-book" in stderr


def test_cli_returns_one_when_live_smoke_returns_failed_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(live_smoke_module, "run_live_smoke", lambda *_args, **_kwargs: {"passed": False})

    code = main(
        [
            "--config",
            str(tmp_path / "smoke.toml"),
            "--env-file",
            str(tmp_path / "smoke.env"),
            "--price-book",
            str(tmp_path / "prices.json"),
            "--output",
            str(tmp_path / "result.json"),
        ]
    )

    assert code == 1


def test_live_smoke_does_not_create_output_on_safety_failure(tmp_path: Path) -> None:
    output = tmp_path / "result.json"
    env_file = _write_env(tmp_path / "smoke.env", prefix="recording")
    price_book = _write_price_book(tmp_path)
    with pytest.raises(LiveSmokeSafetyError):
        run_live_smoke(
            _write_config(tmp_path, database="../escape.db"),
            output,
            env_file=env_file,
            price_book=price_book,
            limits=LIMITS,
        )
    assert not output.exists()


def test_result_schema_rejects_unapproved_content_field() -> None:
    schema = json.loads(RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    assert "content" not in schema["properties"]


def test_recording_live_smoke_exercises_production_chain_and_validates_result(tmp_path: Path) -> None:
    result, recorder, clients, _config, _price_book = _run_with_recording_providers(tmp_path)

    Draft202012Validator(
        json.loads(RESULT_SCHEMA_PATH.read_text(encoding="utf-8")),
        format_checker=FormatChecker(),
    ).validate(result)
    assert result["provider_kind"] == "external_plugin"
    assert re.fullmatch(r"[0-9a-f]{40}", str(result["core_commit"]))
    assert datetime.fromisoformat(str(result["run_at_utc"])).tzinfo is not None
    assert result["passed"] is True
    assert all(result["checks"].values())
    assert result["counters"]["llm_requests"] <= 10
    assert result["counters"]["embedding_items"] <= 30
    assert result["counters"]["rerank_documents"] <= 100
    assert result["counters"]["cost_microunits"] <= 20_000_000
    assert result["counters"]["active_reservations"] == 0
    assert result["counters"]["ordinary_recall_results"] > 0
    assert result["counters"]["entity_recall_results"] > 0
    assert result["counters"]["temporal_recall_results"] > 0
    assert result["counters"]["preference_recall_results"] > 0
    assert {request["url"].rsplit("/", 1)[-1] for request in recorder.requests} == {
        "llm",
        "embedding",
        "reranker",
    }
    assert clients and all(client.is_closed for client in clients)
    assert json.loads((tmp_path / "result.json").read_text(encoding="utf-8")) == result


@pytest.mark.parametrize(
    "section, required_key",
    (
        ("counters", "claims"),
        ("checks", "reranker_success"),
        ("latency_ms", "ingest"),
    ),
)
def test_result_schema_rejects_missing_fixed_evidence(
    tmp_path: Path,
    section: str,
    required_key: str,
) -> None:
    result, *_rest = _run_with_recording_providers(tmp_path)
    schema = json.loads(RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))
    invalid = deepcopy(result)
    del invalid[section][required_key]

    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(invalid)


@pytest.mark.parametrize("section", ("counters", "checks", "latency_ms"))
def test_result_schema_rejects_empty_fixed_evidence(tmp_path: Path, section: str) -> None:
    result, *_rest = _run_with_recording_providers(tmp_path)
    schema = json.loads(RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))
    invalid = deepcopy(result)
    invalid[section] = {}

    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(invalid)


def test_result_schema_rejects_passed_true_when_any_fixed_check_is_false(tmp_path: Path) -> None:
    result, *_rest = _run_with_recording_providers(tmp_path)
    schema = json.loads(RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))
    invalid = deepcopy(result)
    invalid["checks"]["reranker_success"] = False

    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(invalid)


@pytest.mark.parametrize(
    "section, key, invalid_value",
    (
        ("counters", "claims", 0),
        ("counters", "rerank_documents", 0),
        ("counters", "reranker_successful_recalls", 0),
        ("latency_ms", "ingest", 0),
        ("error_categories", None, []),
    ),
)
def test_result_schema_rejects_passed_true_with_zero_or_empty_execution_evidence(
    tmp_path: Path,
    section: str,
    key: str | None,
    invalid_value: object,
) -> None:
    result, *_rest = _run_with_recording_providers(tmp_path)
    schema = json.loads(RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))
    invalid = deepcopy(result)
    if key is None:
        invalid[section] = invalid_value
    else:
        invalid[section][key] = invalid_value

    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(invalid)


def test_result_schema_requires_exact_commit_length_and_utc_timestamp(tmp_path: Path) -> None:
    result, *_rest = _run_with_recording_providers(tmp_path)
    schema = json.loads(RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())

    wrong_commit = deepcopy(result)
    wrong_commit["core_commit"] = "a" * 41
    with pytest.raises(JsonSchemaValidationError):
        validator.validate(wrong_commit)

    non_utc_time = deepcopy(result)
    non_utc_time["run_at_utc"] = "2026-09-01T08:00:00+08:00"
    with pytest.raises(JsonSchemaValidationError):
        validator.validate(non_utc_time)


def test_explicit_env_and_price_book_override_implicit_adjacent_files(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    implicit_env = _write_env(tmp_path / ".env", prefix="implicit-do-not-read")
    explicit_env = _write_env(tmp_path / "smoke.env")
    price_book = _write_price_book(tmp_path)
    recorder = _RecordingTransport()
    clients: list[httpx.Client] = []
    result = _run_live_smoke(
        config,
        tmp_path / "result.json",
        env_file=explicit_env,
        price_book=price_book,
        limits=LIMITS,
        dependencies=_recording_dependencies(tmp_path, recorder, clients),
    )

    assert result["passed"] is True
    assert recorder.requests
    assert all("explicit-" in str(request["authorization"]) for request in recorder.requests)
    assert all("implicit-do-not-read" not in str(request["authorization"]) for request in recorder.requests)
    assert implicit_env.read_text(encoding="utf-8").startswith("LLM_API_KEY=implicit-do-not-read")


def test_explicit_env_ignores_poisoned_process_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for capability in ("LLM", "EMBEDDING", "RERANKER"):
        monkeypatch.setenv(f"{capability}_API_KEY", f"process-do-not-read-{capability.casefold()}")
    result, recorder, _clients, _config, _price_book = _run_with_recording_providers(tmp_path)

    assert result["passed"] is True
    assert recorder.requests
    assert all("recording-" in str(request["authorization"]) for request in recorder.requests)
    assert all("process-do-not-read" not in str(request["authorization"]) for request in recorder.requests)


def test_explicit_input_symlink_is_rejected_before_provider_calls(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    target_env = _write_env(tmp_path / "target.env", prefix="recording")
    linked_env = tmp_path / "linked.env"
    linked_env.symlink_to(target_env)
    price_book = _write_price_book(tmp_path)
    recorder = _RecordingTransport()
    clients: list[httpx.Client] = []

    with pytest.raises(LiveSmokeSafetyError, match="symlink|unsafe"):
        _run_live_smoke(
            config,
            tmp_path / "result.json",
            env_file=linked_env,
            price_book=price_book,
            limits=LIMITS,
            dependencies=_recording_dependencies(tmp_path, recorder, clients),
        )

    assert recorder.requests == []


def test_explicit_input_parent_escape_is_rejected_before_provider_calls(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    _write_env(tmp_path / "smoke.env", prefix="recording")
    escaped_env = tmp_path / "nested" / ".." / "smoke.env"
    price_book = _write_price_book(tmp_path)
    recorder = _RecordingTransport()
    clients: list[httpx.Client] = []

    with pytest.raises(LiveSmokeSafetyError, match="escape|unsafe"):
        _run_live_smoke(
            config,
            tmp_path / "result.json",
            env_file=escaped_env,
            price_book=price_book,
            limits=LIMITS,
            dependencies=_recording_dependencies(tmp_path, recorder, clients),
        )

    assert recorder.requests == []


@pytest.mark.parametrize("redirected_input", ("config", "env", "price"))
def test_explicit_input_with_symlinked_ancestor_is_rejected_before_provider_calls(
    tmp_path: Path,
    redirected_input: str,
) -> None:
    config = _write_config(tmp_path)
    env_file = _write_env(tmp_path / "smoke.env", prefix="recording")
    price_book = _write_price_book(tmp_path)
    real_directory = tmp_path / "real-inputs"
    real_directory.mkdir()
    linked_directory = tmp_path / "linked-inputs"
    linked_directory.symlink_to(real_directory, target_is_directory=True)
    if redirected_input == "config":
        config = linked_directory / _write_config(real_directory).name
    elif redirected_input == "env":
        env_file = linked_directory / _write_env(real_directory / "redirected.env", prefix="recording").name
    else:
        price_book = linked_directory / _write_price_book(real_directory).name
    recorder = _RecordingTransport()
    clients: list[httpx.Client] = []

    with pytest.raises(LiveSmokeSafetyError, match="symlink|junction|unsafe"):
        _run_live_smoke(
            config,
            tmp_path / "result.json",
            env_file=env_file,
            price_book=price_book,
            limits=LIMITS,
            dependencies=_recording_dependencies(tmp_path, recorder, clients),
        )

    assert recorder.requests == []


def test_input_mutation_is_detected_even_when_pipeline_also_fails(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    env_file = _write_env(tmp_path / "smoke.env", prefix="recording")
    price_book = _write_price_book(tmp_path)

    def mutate_then_fail() -> httpx.Client:
        env_file.write_text("LLM_API_KEY=changed\n", encoding="utf-8")
        raise RuntimeError("controlled client factory failure")

    dependencies = _LiveSmokeDependencies(
        entry_points=(_RecordingEntryPoint(),),
        client_factory=mutate_then_fail,
        temp_parent=tmp_path,
    )

    with pytest.raises(LiveSmokeSafetyError, match="input.*changed"):
        _run_live_smoke(
            config,
            tmp_path / "result.json",
            env_file=env_file,
            price_book=price_book,
            limits=LIMITS,
            dependencies=dependencies,
        )


def test_input_mutation_is_detected_when_early_price_loading_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _write_config(tmp_path)
    env_file = _write_env(tmp_path / "smoke.env", prefix="recording")
    price_book = _write_price_book(tmp_path)

    def mutate_then_fail(_path: Path) -> UsagePriceBook:
        env_file.write_text("LLM_API_KEY=changed\n", encoding="utf-8")
        raise RuntimeError("controlled early price failure")

    monkeypatch.setattr(UsagePriceBook, "load", mutate_then_fail)

    with pytest.raises(LiveSmokeSafetyError, match="input.*changed"):
        _run_live_smoke(
            config,
            tmp_path / "result.json",
            env_file=env_file,
            price_book=price_book,
            limits=LIMITS,
            dependencies=_LiveSmokeDependencies(temp_parent=tmp_path),
        )


def test_input_mutation_during_snapshot_is_rejected_before_provider_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _write_config(tmp_path)
    env_file = _write_env(tmp_path / "smoke.env", prefix="recording")
    price_book = _write_price_book(tmp_path)
    recorder = _RecordingTransport()
    clients: list[httpx.Client] = []
    copyfile = shutil.copyfile

    def copy_then_mutate(source: Path, destination: Path) -> str:
        copied = copyfile(source, destination)
        if Path(source) == env_file:
            env_file.write_text("LLM_API_KEY=changed\n", encoding="utf-8")
        return copied

    monkeypatch.setattr(shutil, "copyfile", copy_then_mutate)

    with pytest.raises(LiveSmokeSafetyError, match="input.*changed"):
        _run_live_smoke(
            config,
            tmp_path / "result.json",
            env_file=env_file,
            price_book=price_book,
            limits=LIMITS,
            dependencies=_recording_dependencies(tmp_path, recorder, clients),
        )

    assert recorder.requests == []


def test_input_mutation_is_detected_when_output_write_also_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _write_config(tmp_path)
    env_file = _write_env(tmp_path / "smoke.env", prefix="recording")
    price_book = _write_price_book(tmp_path)
    recorder = _RecordingTransport()
    clients: list[httpx.Client] = []

    def mutate_then_fail(_output: Path, _result: object) -> None:
        config.write_text("changed = true\n", encoding="utf-8")
        raise OSError("controlled output failure")

    monkeypatch.setattr("benchmarks.provider.live_smoke._atomic_write_json", mutate_then_fail)

    with pytest.raises(LiveSmokeSafetyError, match="input.*changed"):
        _run_live_smoke(
            config,
            tmp_path / "result.json",
            env_file=env_file,
            price_book=price_book,
            limits=LIMITS,
            dependencies=_recording_dependencies(tmp_path, recorder, clients),
        )

    assert not (tmp_path / "result.json").exists()


def test_all_real_reranker_calls_failing_cannot_pass_the_success_check(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    price_book = _write_price_book(tmp_path)
    env_file = _write_env(tmp_path / "smoke.env", prefix="recording")
    recorder = _RecordingTransport(fail_reranker=True)
    clients: list[httpx.Client] = []

    result = _run_live_smoke(
        config,
        tmp_path / "result.json",
        env_file=env_file,
        price_book=price_book,
        limits=LIMITS,
        dependencies=_recording_dependencies(tmp_path, recorder, clients),
    )

    assert result["checks"]["reranker_success"] is False
    assert result["checks"]["reranker_failure_fallback"] is True
    assert result["passed"] is False


def test_result_contains_no_fixture_provider_or_path_content(tmp_path: Path) -> None:
    result, _recorder, _clients, config, price_book = _run_with_recording_providers(tmp_path)
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    encoded = json.dumps(result, ensure_ascii=False)

    for forbidden in (
        fixture["text"],
        *(claim["value"] for claim in fixture["recording_extraction"]["claims"]),
        "Bearer",
        "recording-llm-secret",
        "provider.invalid",
        str(config),
        str(price_book),
        str(tmp_path),
    ):
        assert forbidden not in encoded


@pytest.mark.parametrize(
    "limits, category",
    (
        (LiveSmokeLimits(llm_requests=0), "llm_requests"),
        (LiveSmokeLimits(embedding_items=1), "embedding_items"),
        (LiveSmokeLimits(rerank_documents=1), "rerank_documents"),
        (LiveSmokeLimits(cost_microunits=1), "cost_microunits"),
    ),
)
def test_preflight_rejects_budget_overrun_before_first_call(
    tmp_path: Path,
    limits: LiveSmokeLimits,
    category: str,
) -> None:
    config = _write_config(tmp_path)
    price_book = _write_price_book(tmp_path)
    env_file = _write_env(tmp_path / "smoke.env", prefix="recording")
    recorder = _RecordingTransport()
    clients: list[httpx.Client] = []

    with pytest.raises(LiveSmokeBudgetError, match=category):
        _run_live_smoke(
            config,
            tmp_path / "result.json",
            env_file=env_file,
            price_book=price_book,
            limits=limits,
            dependencies=_recording_dependencies(tmp_path, recorder, clients),
        )

    assert recorder.requests == []
    assert clients == [] or all(client.is_closed for client in clients)
    assert not (tmp_path / "result.json").exists()


def test_preflight_missing_model_price_rule_fails_closed_before_first_call(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    price_book = _write_price_book(tmp_path, omit="reranker")
    env_file = _write_env(tmp_path / "smoke.env", prefix="recording")
    recorder = _RecordingTransport()
    clients: list[httpx.Client] = []

    with pytest.raises(LiveSmokeBudgetError, match="unknown cost"):
        _run_live_smoke(
            config,
            tmp_path / "result.json",
            env_file=env_file,
            price_book=price_book,
            limits=LIMITS,
            dependencies=_recording_dependencies(tmp_path, recorder, clients),
        )

    assert recorder.requests == []


def test_live_smoke_guard_counts_transport_and_logical_retry_allowances_before_send(tmp_path: Path) -> None:
    sends = 0

    def timeout(request: httpx.Request) -> httpx.Response:
        nonlocal sends
        sends += 1
        raise httpx.ConnectTimeout("controlled timeout", request=request)

    guard = live_smoke_module._LiveSmokeReservationGuard(LIMITS)
    governor = UsageGovernor(tmp_path / "usage.db", UsageLimits())
    identity = UsageIdentity(
        ProviderCapability.LLM,
        "extract",
        "recording.plugin",
        "recording",
        "recording-llm",
    )
    with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
        governed = GovernedProviderCall(
            identity,
            governor,
            ProviderTransport(client, sleep=lambda _delay: None),
            _reservation_guard=guard.reserve,
        )
        for attempts in (3, 3, 4):
            with pytest.raises(ProviderCallError):
                governed.execute(
                    ProviderRequest("POST", "https://provider.invalid/llm", {}, {}, 1.0),
                    UsageAmount(requests=1, cost_microunits=1),
                    lambda _response: ("unused", UsageAmount(requests=1, cost_microunits=1)),
                    max_attempts=attempts,
                )

        with pytest.raises(LiveSmokeBudgetError, match="llm_requests"):
            governed.execute(
                ProviderRequest("POST", "https://provider.invalid/llm", {}, {}, 1.0),
                UsageAmount(requests=1, cost_microunits=1),
                lambda _response: ("unused", UsageAmount(requests=1, cost_microunits=1)),
                max_attempts=1,
            )

    assert sends == LIMITS.llm_requests
    assert governor.snapshot()["reserved"]["requests"] == 0


@pytest.mark.parametrize(
    ("capability", "amount", "limits", "category"),
    (
        (
            ProviderCapability.EMBEDDING,
            UsageAmount(requests=1, embedding_items=2, cost_microunits=1),
            LiveSmokeLimits(embedding_items=3),
            "embedding_items",
        ),
        (
            ProviderCapability.RERANKER,
            UsageAmount(requests=1, rerank_documents=2, cost_microunits=1),
            LiveSmokeLimits(rerank_documents=3),
            "rerank_documents",
        ),
        (
            ProviderCapability.LLM,
            UsageAmount(requests=1, cost_microunits=2),
            LiveSmokeLimits(cost_microunits=3),
            "cost_microunits",
        ),
    ),
)
def test_live_smoke_guard_denies_each_scaled_capability_limit_before_second_send(
    tmp_path: Path,
    capability: ProviderCapability,
    amount: UsageAmount,
    limits: LiveSmokeLimits,
    category: str,
) -> None:
    sends = 0

    def success(request: httpx.Request) -> httpx.Response:
        nonlocal sends
        sends += 1
        return httpx.Response(200, request=request, json={"ok": True})

    guard = live_smoke_module._LiveSmokeReservationGuard(limits)
    governor = UsageGovernor(tmp_path / "usage.db", UsageLimits())
    identity = UsageIdentity(capability, "test", "recording.plugin", "recording", "recording-model")
    with httpx.Client(transport=httpx.MockTransport(success)) as client:
        governed = GovernedProviderCall(
            identity,
            governor,
            ProviderTransport(client),
            _reservation_guard=guard.reserve,
        )
        governed.execute(
            ProviderRequest("POST", "https://provider.invalid/run", {}, {}, 1.0),
            amount,
            lambda _response: ("ok", amount),
            max_attempts=1,
        )
        with pytest.raises(LiveSmokeBudgetError, match=category):
            governed.execute(
                ProviderRequest("POST", "https://provider.invalid/run", {}, {}, 1.0),
                amount,
                lambda _response: ("unreachable", amount),
                max_attempts=1,
            )

    assert sends == 1


def test_schema_retry_is_denied_when_its_transport_allowance_would_exceed_smoke_limit(tmp_path: Path) -> None:
    config = _write_config(tmp_path, llm_max_attempts=10)
    price_book = _write_price_book(tmp_path)
    env_file = _write_env(tmp_path / "smoke.env", prefix="recording")
    recorder = _RecordingTransport(malformed_llm=True)
    clients: list[httpx.Client] = []

    result = _run_live_smoke(
        config,
        tmp_path / "result.json",
        env_file=env_file,
        price_book=price_book,
        limits=LIMITS,
        dependencies=_recording_dependencies(tmp_path, recorder, clients),
    )

    llm_requests = [request for request in recorder.requests if str(request["url"]).endswith("/llm")]
    assert len(llm_requests) == 1
    assert result["passed"] is False
    assert result["error_categories"] == ["provider_pipeline_failure"]


def test_first_call_guard_failure_writes_no_artifact(tmp_path: Path) -> None:
    config = _write_config(tmp_path, llm_max_attempts=10)
    price_book = _write_price_book(tmp_path)
    env_file = _write_env(tmp_path / "smoke.env", prefix="recording")
    recorder = _RecordingTransport()
    clients: list[httpx.Client] = []
    output = tmp_path / "result.json"

    with pytest.raises(LiveSmokeBudgetError, match="llm_requests"):
        _run_live_smoke(
            config,
            output,
            env_file=env_file,
            price_book=price_book,
            limits=LiveSmokeLimits(llm_requests=5),
            dependencies=_recording_dependencies(tmp_path, recorder, clients),
        )

    assert recorder.requests == []
    assert not output.exists()


def test_success_evidence_accumulates_actual_transport_retries(tmp_path: Path) -> None:
    config = _write_config(tmp_path, llm_max_attempts=2)
    price_book = _write_price_book(tmp_path)
    env_file = _write_env(tmp_path / "smoke.env", prefix="recording")
    recorder = _RecordingTransport(llm_failures_before_success=1)
    clients: list[httpx.Client] = []

    result = _run_live_smoke(
        config,
        tmp_path / "result.json",
        env_file=env_file,
        price_book=price_book,
        limits=LIMITS,
        dependencies=_recording_dependencies(tmp_path, recorder, clients),
    )

    assert result["passed"] is True
    assert result["counters"]["llm_requests"] == 2
    assert recorder.llm_attempts == 2


def test_price_book_source_url_userinfo_is_rejected_without_disclosure(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    price_book = _write_price_book(tmp_path, source_url="https://do-not-log@pricing.example.test/provider")
    env_file = _write_env(tmp_path / "smoke.env", prefix="recording")
    recorder = _RecordingTransport()
    clients: list[httpx.Client] = []

    with pytest.raises(LiveSmokeSafetyError, match="source URL") as captured:
        _run_live_smoke(
            config,
            tmp_path / "result.json",
            env_file=env_file,
            price_book=price_book,
            limits=LIMITS,
            dependencies=_recording_dependencies(tmp_path, recorder, clients),
        )

    assert "do-not-log" not in str(captured.value)
    assert recorder.requests == []


@pytest.mark.parametrize(
    "source_url",
    (
        "https://pricing.example.test/provider?token=do-not-log",
        "https://pricing.example.test/provider#do-not-log",
        "https://pricing.example.test/provider?",
        "https://pricing.example.test/provider#",
        "https://pricing.example.test/provider?#",
        "https://[do-not-log",
    ),
)
def test_price_book_source_url_query_or_fragment_is_rejected_without_disclosure(
    tmp_path: Path,
    source_url: str,
) -> None:
    config = _write_config(tmp_path)
    price_book = _write_price_book(tmp_path, source_url=source_url)
    env_file = _write_env(tmp_path / "smoke.env", prefix="recording")
    recorder = _RecordingTransport()
    clients: list[httpx.Client] = []

    with pytest.raises(LiveSmokeSafetyError, match="source URL") as captured:
        _run_live_smoke(
            config,
            tmp_path / "result.json",
            env_file=env_file,
            price_book=price_book,
            limits=LIMITS,
            dependencies=_recording_dependencies(tmp_path, recorder, clients),
        )

    assert "do-not-log" not in str(captured.value)
    assert recorder.requests == []


def test_post_attempt_unknown_cost_fails_closed_and_writes_sanitized_failure_output(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    price_book = _write_price_book(tmp_path, embedding_token_rate=1_000_000)
    env_file = _write_env(tmp_path / "smoke.env", prefix="recording")
    recorder = _RecordingTransport(unknown_embedding_usage=True)
    clients: list[httpx.Client] = []

    output = tmp_path / "result.json"
    result = _run_live_smoke(
        config,
        output,
        env_file=env_file,
        price_book=price_book,
        limits=LIMITS,
        dependencies=_recording_dependencies(tmp_path, recorder, clients),
    )

    assert recorder.requests
    assert all(client.is_closed for client in clients)
    assert result["passed"] is False
    assert result["error_categories"] == ["provider_pipeline_failure"]
    assert json.loads(output.read_text(encoding="utf-8")) == result
    Draft202012Validator(
        json.loads(RESULT_SCHEMA_PATH.read_text(encoding="utf-8")),
        format_checker=FormatChecker(),
    ).validate(result)
    encoded = json.dumps(result, ensure_ascii=False)
    for forbidden in (
        "recording-llm-secret",
        "recording-embedding-secret",
        "provider.invalid",
        str(config),
        str(price_book),
        str(tmp_path),
    ):
        assert forbidden not in encoded
    assert not list(tmp_path.glob("hl-mem-provider-smoke-*"))


def test_paid_pipeline_failure_writes_complete_fixed_evidence(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    price_book = _write_price_book(tmp_path)
    env_file = _write_env(tmp_path / "smoke.env", prefix="recording")
    recorder = _RecordingTransport(invalid_embedding_count=True)
    clients: list[httpx.Client] = []
    output = tmp_path / "result.json"
    before = {
        path: (path.stat().st_size, path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in (config, env_file, price_book)
    }

    result = _run_live_smoke(
        config,
        output,
        env_file=env_file,
        price_book=price_book,
        limits=LIMITS,
        dependencies=_recording_dependencies(tmp_path, recorder, clients),
    )
    schema = json.loads(RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))

    Draft202012Validator(schema, format_checker=FormatChecker()).validate(result)
    assert result["passed"] is False
    assert set(result["counters"]) == set(schema["properties"]["counters"]["required"])
    assert set(result["checks"]) == set(schema["properties"]["checks"]["required"])
    assert set(result["latency_ms"]) == set(schema["properties"]["latency_ms"]["required"])
    assert result["counters"]["llm_requests"] >= 1
    assert result["counters"]["embedding_items"] >= 1
    assert result["counters"]["active_reservations"] == 0
    assert result["error_categories"] == ["provider_pipeline_failure"]
    assert json.loads(output.read_text(encoding="utf-8")) == result
    after = {
        path: (path.stat().st_size, path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in (config, env_file, price_book)
    }
    assert after == before


def test_failure_artifact_is_written_before_temporary_ledger_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _write_config(tmp_path)
    price_book = _write_price_book(tmp_path)
    env_file = _write_env(tmp_path / "smoke.env", prefix="recording")
    recorder = _RecordingTransport(invalid_embedding_count=True)
    clients: list[httpx.Client] = []
    atomic_write = live_smoke_module._atomic_write_json
    observed_live_ledger = False

    def record_ledger_then_write(output: Path, result: dict[str, object]) -> None:
        nonlocal observed_live_ledger
        temporary_roots = list(tmp_path.glob("hl-mem-provider-smoke-*"))
        observed_live_ledger = any(list(root.glob("*.budget.db")) for root in temporary_roots)
        atomic_write(output, result)

    monkeypatch.setattr(live_smoke_module, "_atomic_write_json", record_ledger_then_write)

    result = _run_live_smoke(
        config,
        tmp_path / "result.json",
        env_file=env_file,
        price_book=price_book,
        limits=LIMITS,
        dependencies=_recording_dependencies(tmp_path, recorder, clients),
    )

    assert result["passed"] is False
    assert observed_live_ledger is True


def test_live_smoke_preserves_inputs_and_removes_temporary_root(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    price_book = _write_price_book(tmp_path)
    env_file = _write_env(tmp_path / "smoke.env", prefix="recording")
    before = {
        path: (path.stat().st_size, path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in (config, env_file, price_book)
    }
    recorder = _RecordingTransport()
    clients: list[httpx.Client] = []

    _run_live_smoke(
        config,
        tmp_path / "result.json",
        env_file=env_file,
        price_book=price_book,
        limits=LIMITS,
        dependencies=_recording_dependencies(tmp_path, recorder, clients),
    )

    after = {
        path: (path.stat().st_size, path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in (config, env_file, price_book)
    }
    assert after == before
    assert not list(tmp_path.glob("hl-mem-provider-smoke-*"))


def test_wheel_check_rejects_provider_smoke_runtime_artifacts(tmp_path: Path) -> None:
    wheel = tmp_path / "hl_mem.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for member in (
            "hl_mem/evaluation/runner.py",
            "provider-live-smoke-result.json",
            "var/provider-smoke.db",
            "external_plugins/recording_provider.py",
        ):
            archive.writestr(member, "")

    violations = check_wheel(wheel)

    assert any("live smoke result" in violation for violation in violations)
    assert any("temporary database" in violation for violation in violations)
    assert any("external plugin" in violation for violation in violations)
