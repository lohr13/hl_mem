"""Plain-chat transport and retry policy for LongMemEval reader/judge calls."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from typing import Any, TypeVar, cast

import httpx

from hl_mem.http_utils import find_http_exception, find_http_status_error, retry_http

T = TypeVar("T")


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
    return os.environ.get("HL_MEM_EVAL_QA_MODEL") or default_model


def qa_dashscope_chat(
    api_key: str,
    base_url: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    *,
    temperature: float = 0.1,
) -> tuple[str, int]:
    """Call a DashScope-compatible chat completion without structured output."""
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
        "max_tokens": 512,
    }
    with httpx.Client(timeout=60.0) as client:
        response = client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()

    answer_text = ""
    choices = data.get("choices") or []
    if choices:
        answer_text = (choices[0].get("message") or {}).get("content") or ""
    total_tokens = (data.get("usage") or {}).get("total_tokens", 0)
    return str(answer_text), int(total_tokens)


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
