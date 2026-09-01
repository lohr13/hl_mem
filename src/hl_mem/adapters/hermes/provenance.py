"""Deterministic provenance projection from public Hermes hook metadata."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from hl_mem.domain.provenance import OriginClass, SessionKind

MAX_EXTERNAL_TOOL_NAMES = 8
MAX_EXTERNAL_TOOL_NAME_LENGTH = 64
_UNSAFE_TOOL_NAME = re.compile(r"[^A-Za-z0-9_.:-]+")
_UNTRUSTED_WRAPPER = "<untrusted_tool_result"


@dataclass(frozen=True)
class HermesTurnProvenance:
    session_kind: SessionKind
    user_origin: OriginClass
    assistant_origin: OriginClass
    external_tools: tuple[str, ...]


def session_kind_from_host(platform: object, agent_context: object) -> SessionKind:
    """Map only host-declared lifecycle metadata; unknown contexts stay unknown."""
    normalized_platform = platform.strip().lower() if isinstance(platform, str) else ""
    normalized_context = agent_context.strip().lower() if isinstance(agent_context, str) else ""
    if normalized_context == "subagent":
        return "subagent"
    if normalized_context == "cron" or normalized_platform == "cron":
        return "cron"
    if normalized_context == "flush" or normalized_platform == "heartbeat":
        return "heartbeat"
    if normalized_context == "primary":
        return "interactive"
    return "unknown"


def _after_latest_user(messages: Sequence[object]) -> Sequence[object]:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, Mapping) and message.get("role") == "user":
            return messages[index + 1 :]
    return messages


def _contains_wrapper(content: object) -> bool:
    if isinstance(content, str):
        return _UNTRUSTED_WRAPPER in content.lower()
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
        return any(
            _contains_wrapper(item.get("text"))
            for item in content
            if isinstance(item, Mapping) and item.get("type") == "text"
        )
    return False


def _is_external_tool_message(message: Mapping[object, object]) -> bool:
    if message.get("role") != "tool":
        return False
    risk = message.get("_tool_output_risk")
    if isinstance(risk, Mapping) or risk is True:
        return True
    return _contains_wrapper(message.get("content"))


def _safe_tool_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return "unknown_tool"
    sanitized = _UNSAFE_TOOL_NAME.sub("_", value.strip()).strip("_")
    return (sanitized or "unknown_tool")[:MAX_EXTERNAL_TOOL_NAME_LENGTH]


def _external_tools(messages: Sequence[object]) -> tuple[str, ...]:
    names: list[str] = []
    for item in _after_latest_user(messages):
        if not isinstance(item, Mapping) or not _is_external_tool_message(item):
            continue
        name = _safe_tool_name(item.get("name") or item.get("tool_name"))
        if name not in names:
            names.append(name)
        if len(names) == MAX_EXTERNAL_TOOL_NAMES:
            break
    return tuple(names)


def derive_turn_provenance(
    messages: Sequence[object],
    *,
    platform: object,
    agent_context: object,
) -> HermesTurnProvenance:
    """Project one Hermes turn without retaining or interpreting tool output."""
    session_kind = session_kind_from_host(platform, agent_context)
    external_tools = _external_tools(messages)
    if session_kind == "unknown":
        user_origin: OriginClass = "unknown"
        assistant_origin: OriginClass = "unknown"
    else:
        user_origin = "direct_user" if session_kind == "interactive" else "system"
        assistant_origin = "agent"
    if external_tools:
        assistant_origin = "external_derived"
    return HermesTurnProvenance(
        session_kind=session_kind,
        user_origin=user_origin,
        assistant_origin=assistant_origin,
        external_tools=external_tools,
    )
