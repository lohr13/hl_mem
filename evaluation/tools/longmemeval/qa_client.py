"""Plain-chat transport and retry policy for LongMemEval reader/judge calls."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar, cast

import httpx

from hl_mem.http_utils import find_http_exception, find_http_status_error, retry_http

T = TypeVar("T")


@dataclass(frozen=True)
class QAUsage:
    """Normalized token usage from one reader or judge request."""

    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_tokens: int

    @property
    def answer_tokens(self) -> int:
        """Return non-reasoning completion tokens without going negative."""
        return max(0, self.output_tokens - self.reasoning_tokens)


def response_object(content: str | dict[str, Any]) -> dict[str, Any]:
    """Decode a plain or fenced JSON object response."""
    if isinstance(content, dict):
        return content
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]) if len(lines) >= 3 else text
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("LLM structured response must be a JSON object")
    return payload


def qa_model(default_model: str) -> str:
    """Resolve the optional evaluation-only model override."""
    override = os.environ.get("HL_MEM_EVAL_QA_MODEL", "").strip()
    return override or default_model


def qa_dashscope_chat(
    api_key: str,
    base_url: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    *,
    temperature: float = 0.1,
    enable_thinking: bool = False,
    thinking_budget: int | None = None,
    json_object: bool = False,
    max_tokens: int = 512,
) -> tuple[str, int]:
    """Call a DashScope-compatible chat completion with evaluation-safe options."""
    answer_text, usage = qa_dashscope_chat_detailed(
        api_key,
        base_url,
        model,
        system_prompt,
        user_prompt,
        temperature=temperature,
        enable_thinking=enable_thinking,
        thinking_budget=thinking_budget,
        json_object=json_object,
        max_tokens=max_tokens,
    )
    return answer_text, usage.total_tokens


def qa_dashscope_chat_detailed(
    api_key: str,
    base_url: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    *,
    temperature: float = 0.1,
    enable_thinking: bool = False,
    thinking_budget: int | None = None,
    json_object: bool = False,
    max_tokens: int = 512,
    timeout_seconds: float = 60.0,
) -> tuple[str, QAUsage]:
    """Call plain chat and preserve provider token accounting for controls."""
    if max_tokens < 1:
        raise ValueError("max_tokens must be positive")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "enable_thinking": enable_thinking,
    }
    if thinking_budget is not None:
        payload["thinking_budget"] = thinking_budget
    if json_object:
        payload["response_format"] = {"type": "json_object"}
    with httpx.Client(timeout=timeout_seconds) as client:
        response = client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()

    answer_text = ""
    choices = data.get("choices") or []
    if choices:
        answer_text = (choices[0].get("message") or {}).get("content") or ""
    raw_usage = data.get("usage") or {}
    input_tokens = int(raw_usage.get("prompt_tokens", raw_usage.get("input_tokens", 0)) or 0)
    output_tokens = int(raw_usage.get("completion_tokens", raw_usage.get("output_tokens", 0)) or 0)
    completion_details = raw_usage.get("completion_tokens_details") or raw_usage.get("output_tokens_details") or {}
    reasoning_tokens = int(completion_details.get("reasoning_tokens", 0) or 0)
    total_tokens = int(raw_usage.get("total_tokens", 0) or 0)
    if total_tokens == 0:
        total_tokens = input_tokens + output_tokens
    return str(answer_text), QAUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
    )


def _print_retry(attempt: int, max_attempts: int, delay: float, error: BaseException) -> None:
    status_error = find_http_status_error(error)
    if status_error is not None:
        label = f"HTTP {status_error.response.status_code}"
    else:
        timeout_error = find_http_exception(error, (httpx.ReadTimeout, httpx.ConnectTimeout))
        label = type(timeout_error).__name__ if timeout_error is not None else type(error).__name__
    print(f"QA {label} retry {attempt + 1}/{max_attempts} in {delay:g}s", flush=True)


def qa_call_with_retry(
    call: Callable[[], T],
    *,
    max_attempts: int,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Retry reader/judge calls with the shared HTTP policy and QA-compatible settings."""
    return cast(
        T,
        retry_http(
            call,
            max_attempts=max_attempts,
            base_delay=2.0,
            backoff_factor=2.0,
            retry_after=True,
            retry_timeout_types=(httpx.ReadTimeout, httpx.ConnectTimeout),
            retry_connect_errors=False,
            sleep=sleep,
            on_retry=_print_retry,
        ),
    )
