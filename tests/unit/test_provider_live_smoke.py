from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator, FormatChecker

from benchmarks.provider.live_smoke import (
    LiveSmokeBudgetError,
    LiveSmokeLimits,
    LiveSmokeSafetyError,
    _LiveSmokeDependencies,
    _run_live_smoke,
    run_live_smoke,
)
from hl_mem.llm.types import LLMCapabilities, LLMResponse
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
    def __init__(self, *, unknown_embedding_usage: bool = False) -> None:
        self.requests: list[dict[str, Any]] = []
        self.unknown_embedding_usage = unknown_embedding_usage

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append({"url": str(request.url), "authorization": request.headers.get("Authorization")})
        if request.url.path.endswith("/llm"):
            fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
            return httpx.Response(
                200,
                request=request,
                json={"content": json.dumps(fixture["recording_extraction"], ensure_ascii=False)},
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
                    "vectors": vectors,
                    "input_tokens": None if self.unknown_embedding_usage else len(payload["texts"]) * 4,
                },
            )
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


def _recording_dependencies(tmp_path: Path, recorder: _RecordingTransport, clients: list[httpx.Client]):
    def client_factory() -> httpx.Client:
        client = httpx.Client(transport=httpx.MockTransport(recorder))
        clients.append(client)
        return client

    return _LiveSmokeDependencies(
        entry_points=(_RecordingEntryPoint(),),
        client_factory=client_factory,
        environ={
            "LLM_API_KEY": "recording-llm-secret",
            "EMBEDDING_API_KEY": "recording-embedding-secret",
            "RERANKER_API_KEY": "recording-reranker-secret",
        },
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
    recorder = _RecordingTransport()
    clients: list[httpx.Client] = []
    result = _run_live_smoke(
        config,
        tmp_path / "result.json",
        limits=limits,
        dependencies=_recording_dependencies(tmp_path, recorder, clients),
    )
    return result, recorder, clients, config, price_book


def _write_config(tmp_path: Path, *, database: str = "smoke.db", body: str = "") -> Path:
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
max_attempts = 1

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
    with pytest.raises(LiveSmokeSafetyError, match="temporary root"):
        run_live_smoke(config, tmp_path / "result.json", limits=LIMITS)


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

    with pytest.raises(LiveSmokeSafetyError, match="Fake"):
        run_live_smoke(config, tmp_path / "result.json", limits=LIMITS)


def test_live_smoke_requires_a_price_book(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    config.write_text(
        config.read_text(encoding="utf-8").replace('price_book_path = "prices.json"', ""),
        encoding="utf-8",
    )

    with pytest.raises(LiveSmokeSafetyError, match="price book"):
        run_live_smoke(config, tmp_path / "result.json", limits=LIMITS)


def test_live_smoke_does_not_create_output_on_safety_failure(tmp_path: Path) -> None:
    output = tmp_path / "result.json"
    with pytest.raises(LiveSmokeSafetyError):
        run_live_smoke(_write_config(tmp_path, database="../escape.db"), output, limits=LIMITS)
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
    _write_price_book(tmp_path)
    recorder = _RecordingTransport()
    clients: list[httpx.Client] = []

    with pytest.raises(LiveSmokeBudgetError, match=category):
        _run_live_smoke(
            config,
            tmp_path / "result.json",
            limits=limits,
            dependencies=_recording_dependencies(tmp_path, recorder, clients),
        )

    assert recorder.requests == []
    assert clients == [] or all(client.is_closed for client in clients)
    assert not (tmp_path / "result.json").exists()


def test_preflight_missing_model_price_rule_fails_closed_before_first_call(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    _write_price_book(tmp_path, omit="reranker")
    recorder = _RecordingTransport()
    clients: list[httpx.Client] = []

    with pytest.raises(LiveSmokeBudgetError, match="unknown cost"):
        _run_live_smoke(
            config,
            tmp_path / "result.json",
            limits=LIMITS,
            dependencies=_recording_dependencies(tmp_path, recorder, clients),
        )

    assert recorder.requests == []


def test_price_book_source_url_userinfo_is_rejected_without_disclosure(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    _write_price_book(tmp_path, source_url="https://do-not-log@pricing.example.test/provider")
    recorder = _RecordingTransport()
    clients: list[httpx.Client] = []

    with pytest.raises(LiveSmokeSafetyError, match="source URL") as captured:
        _run_live_smoke(
            config,
            tmp_path / "result.json",
            limits=LIMITS,
            dependencies=_recording_dependencies(tmp_path, recorder, clients),
        )

    assert "do-not-log" not in str(captured.value)
    assert recorder.requests == []


def test_final_unknown_cost_fails_closed_and_writes_no_output(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    _write_price_book(tmp_path, embedding_token_rate=1_000_000)
    recorder = _RecordingTransport(unknown_embedding_usage=True)
    clients: list[httpx.Client] = []

    with pytest.raises(LiveSmokeBudgetError, match="unknown cost"):
        _run_live_smoke(
            config,
            tmp_path / "result.json",
            limits=LIMITS,
            dependencies=_recording_dependencies(tmp_path, recorder, clients),
        )

    assert recorder.requests
    assert all(client.is_closed for client in clients)
    assert not (tmp_path / "result.json").exists()
    assert not list(tmp_path.glob("hl-mem-provider-smoke-*"))


def test_live_smoke_preserves_inputs_and_removes_temporary_root(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    price_book = _write_price_book(tmp_path)
    before = {
        path: (path.stat().st_size, path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in (config, price_book)
    }
    recorder = _RecordingTransport()
    clients: list[httpx.Client] = []

    _run_live_smoke(
        config,
        tmp_path / "result.json",
        limits=LIMITS,
        dependencies=_recording_dependencies(tmp_path, recorder, clients),
    )

    after = {
        path: (path.stat().st_size, path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in (config, price_book)
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
