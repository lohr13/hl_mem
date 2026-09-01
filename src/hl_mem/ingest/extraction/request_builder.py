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
from ..extractors import ExtractedClaim

DELTA_REPAIR_SYSTEM_PROMPTS: dict[Literal["zh", "en"], str] = {
    "zh": """你是记忆事实增量修复器。只补提取已接受列表尚未覆盖的原子事实。

严格遵守响应 JSON Schema，只输出 JSON，不要输出解释、Markdown 或额外字段。
- 事实只能来自 <repair_source>，<covered_claims> 仅用于判重。
- 与 covered_claims 语义相同、近义改写、包含关系或仅措辞不同的事实都视为已覆盖，禁止输出。
- 每条 claim 只表达一个原子事实，并保留原文中的主体、专名、数值、单位和 evidence_quote。
- 没有新事实时返回 {"claims":[],"should_memorize":false}。
- 最多输出 20 条新 claim。""",
    "en": """You repair gaps in atomic memory extraction. Emit only facts not covered by the accepted list.

Follow the response JSON Schema exactly. Return JSON only, with no explanation, Markdown, or extra fields.
- Facts must come only from <repair_source>; <covered_claims> is only for duplicate avoidance.
- Semantically equivalent facts, paraphrases, containment, and wording-only variants are already covered and forbidden.
- Each claim states one atomic fact and preserves source subjects, names, numbers, units, and evidence_quote.
- If there are no new facts, return {"claims":[],"should_memorize":false}.
- Return at most 20 new claims.""",
}


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


def build_delta_repair_request(
    chunk: ExtractionChunk,
    context: str,
    occurred_at: str,
    language: Literal["zh", "en"],
    covered_claims: list[ExtractedClaim],
    schema: dict[str, Any],
    structured_mode: StructuredOutputMode,
) -> LLMRequest:
    covered_lines = "\n".join(
        f"{index}. {_compact(claim.subject)} | {_compact(str(claim.value))}"
        for index, claim in enumerate(covered_claims, start=1)
    )
    if language == "en":
        user_prompt = (
            f"Event occurred at: {occurred_at}\n"
            f"Event context (not evidence): {context}\n"
            "<repair_source>\n"
            f"{chunk.text}\n"
            "</repair_source>\n"
            "<covered_claims>\n"
            f"{covered_lines}\n"
            "</covered_claims>\n"
            "Extract only new atomic facts not covered by the list above. Do not repeat covered facts."
        )
    else:
        user_prompt = (
            f"事件发生时间 occurred_at：{occurred_at}\n"
            f"事件上下文（不可作为证据）：{context}\n"
            "<repair_source>\n"
            f"{chunk.text}\n"
            "</repair_source>\n"
            "<covered_claims>\n"
            f"{covered_lines}\n"
            "</covered_claims>\n"
            "只提取上述列表未覆盖的新原子事实，禁止复述。"
        )
    return LLMRequest(
        messages=[
            LLMMessage(role="system", content=DELTA_REPAIR_SYSTEM_PROMPTS[language]),
            LLMMessage(role="user", content=user_prompt),
        ],
        structured_output=StructuredOutputSpec(
            name="delta_repair_response",
            schema=schema,
            preferred_mode=structured_mode,
        ),
    )


def _compact(value: str) -> str:
    return " ".join(value.split())


__all__ = ["build_delta_repair_request", "build_extraction_request"]
