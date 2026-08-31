"""External wheel fixture that uses only the public hl_mem.plugins surface."""

from __future__ import annotations

from hl_mem.plugins import (
    PROVIDER_API_VERSION,
    LLMCapabilities,
    LLMInvocation,
    LLMResponse,
    ProviderCallError,
    ProviderCapability,
    ProviderCapabilitySpec,
    ProviderEndpoint,
    ProviderKey,
    ProviderManifest,
    ProviderPlugin,
    ProviderRequest,
    ProviderResponse,
    ProviderStability,
)


class FixtureLLMProvider:
    capabilities = LLMCapabilities(json_object=True, json_schema_strict=False)

    def build_request(self, endpoint: ProviderEndpoint, invocation: LLMInvocation) -> ProviderRequest:
        return ProviderRequest(
            "POST",
            f"{endpoint.base_url.rstrip('/')}/fixture",
            {"Authorization": f"Bearer {endpoint.api_key}"},
            {"model": endpoint.model, "mode": invocation.mode.value},
            endpoint.timeout_seconds,
        )

    def parse_response(self, response: ProviderResponse) -> LLMResponse:
        return LLMResponse(str(response.json_body.get("content", "")), "stop", 0)

    def is_structured_mode_unsupported(self, error: ProviderCallError) -> bool:
        return False


def _plugin(plugin_id: str, provider_name: str) -> ProviderPlugin:
    key = ProviderKey(ProviderCapability.LLM, provider_name)
    manifest = ProviderManifest(
        id=plugin_id,
        version="1.0.0",
        api_version=PROVIDER_API_VERSION,
        requires_hl_mem=">=0.36.1,<2",
        capabilities=(ProviderCapabilitySpec(provider_name, ProviderCapability.LLM, ProviderStability.STABLE),),
        config_schema={
            "type": "object",
            "properties": {"region": {"type": "string"}},
            "additionalProperties": False,
        },
    )
    return ProviderPlugin(manifest, {key: lambda _context: FixtureLLMProvider()})


def plugin() -> ProviderPlugin:
    return _plugin("fixture.provider", "fixture_llm")


def conflict_plugin() -> ProviderPlugin:
    return _plugin("fixture.conflict", "dashscope")
