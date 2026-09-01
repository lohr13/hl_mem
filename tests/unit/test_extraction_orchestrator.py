from __future__ import annotations

from dataclasses import dataclass

from hl_mem.ingest.chunking import ChunkingPolicy
from hl_mem.ingest.extraction.orchestrator import (
    ExtractionOrchestrator,
    ExtractionOrchestratorConfig,
    ExtractionOrchestratorHooks,
)
from hl_mem.ingest.extraction.schema import temporal_gate_extraction_response_json_schema
from hl_mem.llm.types import LLMRequest, LLMResponse, StructuredOutputMode


@dataclass
class _Client:
    responses: list[LLMResponse]

    def __post_init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return self.responses.pop(0)


def _response(content: str, finish_reason: str = "stop") -> LLMResponse:
    return LLMResponse(content, finish_reason, 7, input_tokens=4, output_tokens=3)


def _orchestrator(client: _Client) -> ExtractionOrchestrator:
    return ExtractionOrchestrator(
        client=client,
        provider_name="test",
        model="test-model",
        config=ExtractionOrchestratorConfig(
            chunking_policy=ChunkingPolicy(10_000, 0, 2),
            schema_retries=1,
            structured_mode=StructuredOutputMode.JSON_SCHEMA,
            soft_split_enabled=False,
            delta_repair_enabled=False,
        ),
        hooks=ExtractionOrchestratorHooks(
            bind_run_state=lambda _state: None,
            project_claims=lambda _result, _chunk, _context, _occurred_at: [],
            verify_claims=lambda claims, _source: claims,
            postprocess_claims=lambda claims: claims,
            system_prompt_for_language=lambda language: f"system:{language}",
            response_json_schema=temporal_gate_extraction_response_json_schema,
            language_detector=lambda _text: "en",
            legacy_claim_defaults={},
            kind_values=set(),
            notability_values=set(),
        ),
        verification_enabled=False,
    )


def test_orchestrator_owns_run_state_and_usage_accounting() -> None:
    client = _Client([_response('{"claims":[],"should_memorize":false}')])

    result = _orchestrator(client).extract("ordinary source")

    assert result.claims == ()
    assert result.state.llm_call_count == 1
    assert (result.state.input_tokens, result.state.output_tokens, result.state.total_tokens) == (4, 3, 7)


def test_schema_retry_remains_inside_the_orchestrator() -> None:
    client = _Client(
        [
            _response("not-json"),
            _response('{"claims":[],"should_memorize":false}'),
        ]
    )

    result = _orchestrator(client).extract("ordinary source")

    assert result.claims == ()
    assert result.state.llm_call_count == 2
    assert result.state.schema_retry_count == 1
    assert len(client.requests) == 2
