"""Small deterministic helpers for Hermes payload construction."""

from __future__ import annotations

import hashlib
import json
import re

MAX_TRACE_OBSERVATION_SUMMARY_LENGTH = 500
MAX_EPISODE_GOAL_LENGTH = 5_000
EPISODE_GOAL_FALLBACK = "Complete tool-assisted task"
_ERROR_PATTERNS = (
    re.compile(r"^Traceback", re.MULTILINE),
    re.compile(r"^Error:", re.MULTILINE),
    re.compile(r"^FAILED\b", re.MULTILINE),
    re.compile(r"\bException\b"),
    re.compile(r"\b(?:[A-Za-z_]\w*)?Error\b(?:[ \t]+[^:\r\n]+)?:"),
)
_EXIT_CODE_PATTERN = re.compile(r'["\']?exit_code["\']?\s*[:=]\s*(-?\d+)')


def summarize_observation(raw: str) -> str:
    """Return a bounded status-prefixed trace observation."""
    if not raw:
        return ""
    exit_codes = (int(match.group(1)) for match in _EXIT_CODE_PATTERN.finditer(raw))
    is_error = any(pattern.search(raw) for pattern in _ERROR_PATTERNS) or any(code != 0 for code in exit_codes)
    status = "error" if is_error else "success"
    summary = raw[:MAX_TRACE_OBSERVATION_SUMMARY_LENGTH].strip()
    return f"[{status}] {summary}"


def memory_idempotency_key(key: str, target: str, content: str, namespace: str = "default") -> str:
    """Derive a retry key from host identity and a one-way content digest."""
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    identity = json.dumps([namespace, key, target, content_hash], ensure_ascii=False, separators=(",", ":"))
    return f"hermes-memory:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


def trusted_namespace(namespace: str) -> str:
    """Validate a namespace supplied by trusted host configuration or hook arguments."""
    if not isinstance(namespace, str) or not namespace:
        raise ValueError("namespace must be a non-empty string")
    if len(namespace) > 100:
        raise ValueError("namespace must be at most 100 characters")
    return namespace


def episode_goal(content: str) -> str:
    """Bound the current user content used as an Episode goal."""
    return (content.strip() or EPISODE_GOAL_FALLBACK)[:MAX_EPISODE_GOAL_LENGTH]
