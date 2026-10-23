"""Provider-neutral request construction for the extraction state machine."""

from __future__ import annotations

from typing import Any, Literal

from hl_mem.llm.types import (
    LLMMessage,
    LLMRequest,
    StructuredOutputMode,
    StructuredOutputSpec,
)

from ..chunking import ExtractionChunk


def build_extraction_request(
    chunk: ExtractionChunk,
    context: str,
    occurred_at: str,
    language: Literal["zh", "en"],
    retry_instruction: str,
    system_prompt: str,
    schema: dict[str, Any],
    structured_mode: StructuredOutputMode,
) -> LLMRequest:
    if language == "en":
        user_prompt = (
            f"Event occurred at: {occurred_at}\n"
            f"Event context: {context}\n"
            "<context_only>\n"
            f"{chunk.context_prefix}\n"
            "</context_only>\n"
            "Use context_only only to resolve subjects. Do not extract claims from it.\n"
            "<extract_from>\n"
            f"{chunk.text}\n"
            "</extract_from>"
            f"{retry_instruction}"
        )
    else:
        user_prompt = (
            f"事件发生时间 occurred_at：{occurred_at}\n"
            f"事件上下文：{context}\n"
            "<context_only>\n"
            f"{chunk.context_prefix}\n"
            "</context_only>\n"
            "context_only 仅用于消解主语，禁止从中提取 claim。\n"
            "<extract_from>\n"
            f"{chunk.text}\n"
            "</extract_from>"
            f"{retry_instruction}"
        )
    return LLMRequest(
        messages=[
            LLMMessage(role="system", content=system_prompt),
            LLMMessage(role="user", content=user_prompt),
        ],
        structured_output=StructuredOutputSpec(
            name="extraction_response",
            schema=schema,
            preferred_mode=structured_mode,
        ),
    )


__all__ = ["build_extraction_request"]
