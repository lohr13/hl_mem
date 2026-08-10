"""Shared HTTP diagnostics sanitization for evaluation artifacts."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import httpx

from hl_mem.http_utils import (
    find_http_status_error,
    http_error_diagnostics,
    sanitize_http_response_body,
)

_SENSITIVE_KEY_PARTS = {
    "authorization",
    "credential",
    "credentials",
    "cookie",
    "passphrase",
    "passwd",
    "password",
    "secret",
    "token",
}
_SENSITIVE_KEY_VALUES = {
    "access_key",
    "api_key",
    "client_key",
    "private_key",
    "session_key",
}
_BEARER_RE = re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?bearer\s+)[^\s,\"']+")
_SENSITIVE_KEY_PATTERN = (
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|token|"
    r"password|passwd|passphrase|secret|credential|credentials|cookie|session[_-]?(?:key|token))"
)
_QUOTED_KEY_VALUE_RE = re.compile(
    rf"(?isx)(?P<prefix>[\"']?{_SENSITIVE_KEY_PATTERN}[\"']?\s*[:=]\s*)"
    r"(?P<quote>[\"'])(?P<value>(?:\\.|(?!(?P=quote)).)*)(?P=quote)"
)
_KEY_VALUE_RE = re.compile(rf"(?ix)([\"']?{_SENSITIVE_KEY_PATTERN}[\"']?\s*[:=]\s*)(?![\"'])[^\s,}}\"']+")
_SK_TOKEN_RE = re.compile(r"(?i)\bsk-[A-Za-z0-9][A-Za-z0-9._-]{7,}\b")


def _normalized_key(value: object) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value))
    return re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_")


def _sensitive_key(value: object) -> bool:
    normalized = _normalized_key(value)
    parts = set(normalized.split("_"))
    return normalized.startswith("sk_") or normalized in _SENSITIVE_KEY_VALUES or bool(parts & _SENSITIVE_KEY_PARTS)


def _redact_unstructured(text: str) -> str:
    sanitized = _BEARER_RE.sub(r"\1[REDACTED]", text)
    sanitized = _QUOTED_KEY_VALUE_RE.sub(
        lambda match: f"{match.group('prefix')}{match.group('quote')}[REDACTED]{match.group('quote')}",
        sanitized,
    )
    sanitized = _KEY_VALUE_RE.sub(r"\1[REDACTED]", sanitized)
    return _SK_TOKEN_RE.sub("sk-[REDACTED]", sanitized)


def _redact_sensitive_values(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _sensitive_key(key) else _redact_sensitive_values(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_redact_sensitive_values(item) for item in value]
    if isinstance(value, str):
        return _redact_unstructured(value)
    return value


def sanitize_diagnostic_text(
    text: str,
    *,
    limit: int = 500,
    secrets: Iterable[str] = (),
) -> str:
    """Redact structured sensitive keys and common unstructured credential forms."""
    if limit < 0:
        raise ValueError("diagnostic text limit must be non-negative")
    try:
        payload = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        sanitized_text: str = sanitize_http_response_body(_redact_unstructured(text), limit=limit, secrets=secrets)
        return sanitized_text
    sanitized = json.dumps(_redact_sensitive_values(payload), ensure_ascii=False, separators=(",", ":"))
    for secret in sorted({value for value in secrets if value}, key=len, reverse=True):
        sanitized = sanitized.replace(secret, "[REDACTED]")
    return sanitized[:limit]


def evaluation_http_error_diagnostics(
    error: BaseException,
    *,
    body_limit: int = 500,
    secrets: Iterable[str] = (),
) -> dict[str, Any] | None:
    """Return bounded evaluation diagnostics with the stricter shared sanitizer."""
    secret_values = tuple(value for value in secrets if value)
    raw_diagnostics: object = http_error_diagnostics(error, body_limit=body_limit, secrets=secret_values)
    if raw_diagnostics is None:
        return None
    if not isinstance(raw_diagnostics, Mapping):
        return None
    diagnostics: dict[str, Any] = {str(key): value for key, value in raw_diagnostics.items()}
    status_error = find_http_status_error(error)
    if status_error is not None:
        try:
            diagnostics["response_body"] = sanitize_diagnostic_text(
                status_error.response.text,
                limit=body_limit,
                secrets=secret_values,
            )
        except httpx.ResponseNotRead:
            diagnostics["response_body"] = None
    for field, limit in (("provider_code", 128), ("request_id", 256)):
        value = diagnostics.get(field)
        if value is not None:
            diagnostics[field] = sanitize_diagnostic_text(str(value), limit=limit, secrets=secret_values) or None
    return diagnostics
