"""Chunking, retry, split, and repair orchestration for LLM extraction."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable, Literal, Protocol

from pydantic import ValidationError as PydanticValidationError

from hl_mem.errors import LLMOutputTruncatedError, LLMSchemaValidationError
from hl_mem.llm.types import (
    LLMRequest,
    LLMResponse,
    StructuredOutputMode,
)
from hl_mem.observability.audit import current_audit

from ..chunking import (
    ChunkingPolicy,
    ExtractionChunk,
    bisect_extraction_chunk,
    split_extraction_content,
)
from ..extractors import ExtractedClaim
from .parsing import (
    cap_extraction_claims,
    count_repairs,
    looks_like_truncated_json,
    parse_json_response,
    parse_legacy_defaults,
    schema_error_details,
    schema_error_paths,
    schema_retry_instruction,
    uses_compact_schema,
)
from .postprocessing import merge_chunk_claims
from .repair import repair_extraction_json
from .request_builder import build_extraction_request
from .run_state import ExtractionRunState
from .schema import CompactExtractionResponseSchema, ExtractionResponseSchema

LOGGER = logging.getLogger("hl_mem.ingest.llm_extractor")


class CompletionClient(Protocol):
    def complete(self, request: LLMRequest) -> LLMResponse: ...


ExtractionResponse = CompactExtractionResponseSchema | ExtractionResponseSchema
ProjectClaims = Callable[
    [ExtractionResponse, ExtractionChunk, dict[str, Any], str],
    list[ExtractedClaim],
]


@dataclass(frozen=True, slots=True)
class ExtractionOrchestratorHooks:
    """Product-policy callbacks kept outside the extraction state machine."""

    bind_run_state: Callable[[ExtractionRunState], None]
    project_claims: ProjectClaims
    verify_claims: Callable[[list[ExtractedClaim], str], list[ExtractedClaim]]
    postprocess_claims: Callable[[list[ExtractedClaim]], list[ExtractedClaim]]
    system_prompt_for_language: Callable[[Literal["zh", "en"]], str]
    response_json_schema: Callable[[], dict[str, Any]]
    language_detector: Callable[[str], Literal["zh", "en"]]
    legacy_claim_defaults: Mapping[str, Any]
    kind_values: set[str]
    notability_values: set[str]
    extractor_hash: str | None = None


@dataclass(frozen=True, slots=True)
class ExtractionOrchestratorConfig:
    chunking_policy: ChunkingPolicy
    schema_retries: int
    structured_mode: StructuredOutputMode
    soft_split_enabled: bool
    delta_repair_enabled: bool


@dataclass(frozen=True, slots=True)
class ExtractionRunResult:
    claims: tuple[ExtractedClaim, ...]
    state: ExtractionRunState


class ExtractionOrchestrator:
    """Own the extraction run state machine while leaving Claim policy in the facade."""

    def __init__(
        self,
        *,
        client: CompletionClient,
        provider_name: str,
        model: str,
        config: ExtractionOrchestratorConfig,
        hooks: ExtractionOrchestratorHooks,
        verification_enabled: bool,
    ) -> None:
        if config.schema_retries < 0:
            raise ValueError("schema_retries must be non-negative")
        self.client = client
        self.provider_name = provider_name
        self.model = model
        self.config = config
        self._hooks = hooks
        self._verification_enabled = verification_enabled
        self._state = ExtractionRunState()

    def extract(
        self,
        content: dict[str, Any] | str,
        context: dict[str, Any] | None = None,
    ) -> ExtractionRunResult:
        self._state = ExtractionRunState()
        self._hooks.bind_run_state(self._state)
        event_context = context or {}
        chunks = split_extraction_content(content, self.config.chunking_policy)
        chunk_claims = [self._extract_chunk_with_auto_split(chunk, event_context, depth=0) for chunk in chunks]
        claims = merge_chunk_claims(chunk_claims)
        if not self._verification_enabled:
            claims = self._hooks.postprocess_claims(claims)
        if self._state.secret_rejections:
            current_audit().emit(
                "extract",
                "secret_rejected",
                "rejected",
                detail={
                    "count": sum(self._state.secret_rejections.values()),
                    "reason_counts": self._state.secret_rejections,
                    "extractor_hash": self._hooks.extractor_hash,
                },
            )
        if LOGGER.isEnabledFor(logging.DEBUG):
            LOGGER.debug(
                "%s",
                json.dumps(
                    {
                        "event": "llm_extraction",
                        "actor": event_context.get("actor") or event_context.get("actor_type"),
                        "session_id": event_context.get("session_id"),
                        "content_length": _content_length(content),
                        "should_memorize": bool(claims),
                        "reason": _decision_reason(self._state),
                        "claims_count": len(claims),
                        "schema_retry_count": self._state.schema_retry_count,
                        "repair_count": self._state.repair_count,
                        "llm_call_count": self._state.llm_call_count,
                        "input_tokens": self._state.input_tokens,
                        "output_tokens": self._state.output_tokens,
                        "total_tokens": self._state.total_tokens,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        return ExtractionRunResult(tuple(claims), self._state)

    def _extract_chunk_with_auto_split(
        self,
        chunk: ExtractionChunk,
        event_context: dict[str, Any],
        depth: int,
    ) -> list[ExtractedClaim]:
        try:
            claims = self._extract_one_chunk(chunk, event_context)
            if self._verification_enabled:
                claims = self._hooks.verify_claims(self._hooks.postprocess_claims(claims), chunk.text)
            return claims
        except LLMOutputTruncatedError as error:
            split = bisect_extraction_chunk(chunk)
            if depth >= self.config.chunking_policy.max_split_depth or split is None:
                raise LLMOutputTruncatedError(
                    "LLM output remains truncated after auto split: "
                    f"chunk={chunk.index}, start_unit={chunk.start_unit}, "
                    f"end_unit={chunk.end_unit}, depth={depth}"
                ) from error
            left, right = split
            return merge_chunk_claims(
                [
                    self._extract_chunk_with_auto_split(
                        left,
                        event_context,
                        depth + 1,
                    ),
                    self._extract_chunk_with_auto_split(
                        right,
                        event_context,
                        depth + 1,
                    ),
                ]
            )

    def _extract_one_chunk(
        self,
        chunk: ExtractionChunk,
        event_context: dict[str, Any],
    ) -> list[ExtractedClaim]:
        prompt_context = {key: value for key, value in event_context.items() if not key.startswith("_")}
        context = json.dumps(prompt_context, ensure_ascii=False)
        occurred_at = str(event_context.get("occurred_at", "未知"))
        language = self._hooks.language_detector(chunk.text)
        result = self._request_chunk(chunk, context, occurred_at, language)
        if not result.should_memorize and not result.claims:
            self._state.memorize_decisions.append((False, "should_memorize=false"))
            return []
        if not result.should_memorize:
            current_audit().emit(
                "extract",
                "should_memorize_checked",
                "claims_override_should_memorize_false",
                detail={"claim_count": len(result.claims)},
            )
        claims = self._hooks.project_claims(result, chunk, event_context, occurred_at)
        reasons = sorted({claim.reason for claim in claims if claim.reason})
        self._state.memorize_decisions.append((bool(claims), "；".join(reasons) if claims else "postprocess_rejected"))
        return claims

    def _request_chunk(
        self,
        chunk: ExtractionChunk,
        context: str,
        occurred_at: str,
        language: Literal["zh", "en"],
    ) -> ExtractionResponse:
        errors: list[dict[str, Any]] = []
        previous_output: Any = None
        for attempt in range(self.config.schema_retries + 1):
            if attempt:
                self._state.schema_retry_count += 1
            retry_instruction = schema_retry_instruction(previous_output, errors, language) if errors else ""
            request = build_extraction_request(
                chunk,
                context,
                occurred_at,
                language,
                retry_instruction,
                self._hooks.system_prompt_for_language(language),
                self._hooks.response_json_schema(),
                self.config.structured_mode,
            )
            response = self.client.complete(request)
            self._record_usage(response)
            if response.finish_reason in {"length", "max_tokens"}:
                raise LLMOutputTruncatedError(
                    f"LLM output truncated: provider={self.provider_name}, model={self.model}"
                )
            previous_output_payload: Any = response.content
            try:
                raw = parse_json_response(response.content)
                previous_output_payload = raw
                repaired = repair_extraction_json(raw, provider=self.provider_name, model=self.model)
                self._state.repair_count += count_repairs(raw, repaired)
                budget = cap_extraction_claims(repaired)
                repaired = budget.payload
                if uses_compact_schema(repaired):
                    _add_compact_action_defaults(repaired)
                    result: ExtractionResponse = CompactExtractionResponseSchema.model_validate(repaired)
                else:
                    result = ExtractionResponseSchema.model_validate(
                        parse_legacy_defaults(repaired, self._hooks.legacy_claim_defaults)
                    )
                if budget.dropped_count:
                    current_audit().emit(
                        "extract",
                        "claim_budget",
                        "overflow_truncated",
                        detail={
                            "generated_claim_count": budget.generated_count,
                            "retained_claim_count": budget.retained_count,
                            "dropped_claim_count": budget.dropped_count,
                            "chunk_index": chunk.index,
                            "start_unit": chunk.start_unit,
                            "end_unit": chunk.end_unit,
                        },
                    )
                return result
            except (PydanticValidationError, ValueError) as error:
                if isinstance(error, PydanticValidationError):
                    self._state.schema_errors.extend(dict(item) for item in error.errors())
                if looks_like_truncated_json(response.content):
                    raise LLMOutputTruncatedError(
                        f"LLM output appears truncated: provider={self.provider_name}, model={self.model}"
                    ) from error
                previous_output = previous_output_payload
                errors = schema_error_details(
                    error,
                    previous_output,
                    kind_values=self._hooks.kind_values,
                    notability_values=self._hooks.notability_values,
                )
                if attempt == self.config.schema_retries:
                    raise LLMSchemaValidationError(
                        "LLM response does not contain valid JSON or match schema: "
                        f"provider={self.provider_name}, model={self.model}, "
                        f"chunk_length={len(chunk.text)}, errors={schema_error_paths(error)}"
                    ) from error
        raise RuntimeError("unreachable")

    def _record_usage(self, response: LLMResponse) -> None:
        self._state.llm_call_count += 1
        self._state.total_tokens += response.usage_total_tokens
        self._state.input_tokens += response.input_tokens or 0
        self._state.output_tokens += response.output_tokens or 0


def _add_compact_action_defaults(payload: dict[str, Any]) -> None:
    claims = payload.get("claims")
    if isinstance(claims, list):
        for claim in claims:
            if isinstance(claim, dict):
                claim.setdefault("action", None)
                claim.setdefault("object", None)


def _content_length(content: dict[str, Any] | str) -> int:
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return len(content["text"])
    return len(content) if isinstance(content, str) else len(json.dumps(content, ensure_ascii=False))


def _decision_reason(state: ExtractionRunState) -> str:
    reasons = list(dict.fromkeys(reason for _decision, reason in state.memorize_decisions if reason))
    return "；".join(reasons) or "no_chunks"


__all__ = [
    "ExtractionOrchestrator",
    "ExtractionOrchestratorConfig",
    "ExtractionOrchestratorHooks",
    "ExtractionRunResult",
]
