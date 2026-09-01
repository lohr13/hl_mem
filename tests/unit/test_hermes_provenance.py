"""Deterministic Hermes turn provenance tests."""

from __future__ import annotations

from typing import Any

import pytest

from hl_mem.adapters.hermes.provenance import derive_turn_provenance, session_kind_from_host
from hl_mem.adapters.hermes.provider import HLMemProvider


def test_known_host_contexts_map_without_guessing() -> None:
    assert session_kind_from_host(platform="cli", agent_context="primary") == "interactive"
    assert session_kind_from_host(platform="cron", agent_context="primary") == "cron"
    assert session_kind_from_host(platform="telegram", agent_context="subagent") == "subagent"
    assert session_kind_from_host(platform="heartbeat", agent_context="primary") == "heartbeat"
    assert session_kind_from_host(platform="future-host", agent_context=None) == "unknown"


def test_interactive_turn_has_direct_user_and_agent_origins() -> None:
    provenance = derive_turn_provenance([], platform="cli", agent_context="primary")

    assert provenance.session_kind == "interactive"
    assert provenance.user_origin == "direct_user"
    assert provenance.assistant_origin == "agent"
    assert provenance.external_tools == ()


def test_cron_prompt_is_system_but_assistant_remains_agent() -> None:
    provenance = derive_turn_provenance([], platform="cron", agent_context="primary")

    assert provenance.session_kind == "cron"
    assert provenance.user_origin == "system"
    assert provenance.assistant_origin == "agent"


def test_current_turn_external_tool_taints_assistant_even_for_short_result() -> None:
    messages = [
        {"role": "user", "content": "look it up"},
        {
            "role": "tool",
            "name": "web_search",
            "content": "short",
            "_tool_output_risk": {"risk": "low", "findings": [], "redacted": False},
        },
        {"role": "assistant", "content": "result"},
    ]

    provenance = derive_turn_provenance(messages, platform="cli", agent_context="primary")

    assert provenance.assistant_origin == "external_derived"
    assert provenance.external_tools == ("web_search",)


def test_external_scan_stops_at_latest_user_and_sanitizes_bounded_unique_names() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "tool", "name": "old-web", "_tool_output_risk": {}},
        {"role": "user", "content": "new turn"},
    ]
    for index in range(12):
        messages.append(
            {
                "role": "tool",
                "name": f"web tool/{index}",
                "_tool_output_risk": {"risk": "low"},
            }
        )
    messages.append({"role": "tool", "name": "web tool/0", "_tool_output_risk": {"risk": "high"}})

    provenance = derive_turn_provenance(messages, platform="cli", agent_context="primary")

    assert len(provenance.external_tools) == 8
    assert provenance.external_tools[0] == "web_tool_0"
    assert "old-web" not in provenance.external_tools
    assert len(set(provenance.external_tools)) == len(provenance.external_tools)


def test_wrapper_fallback_and_malformed_messages_are_safe() -> None:
    wrapped = derive_turn_provenance(
        [{"role": "tool", "content": '<untrusted_tool_result source="web_extract">data'}],
        platform="cli",
        agent_context="primary",
    )
    malformed = derive_turn_provenance(
        [None, "text", {"role": 4}, {"role": "tool", "name": {"bad": "name"}}],  # type: ignore[list-item]
        platform=None,
        agent_context=None,
    )

    assert wrapped.assistant_origin == "external_derived"
    assert wrapped.external_tools == ("unknown_tool",)
    assert malformed.session_kind == "unknown"
    assert malformed.user_origin == "unknown"
    assert malformed.assistant_origin == "unknown"


def test_sync_turn_preserves_pair_shape_and_adds_only_bounded_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = HLMemProvider("unused.db", "http://memory.test", timeout=2.0)
    provider.initialize("session-1", platform="cli", agent_context="primary")
    requests: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(provider, "_sync_post", lambda path, payload: requests.append((path, payload)) or True)
    monkeypatch.setattr(provider, "_sync_episode_sync", lambda *_args, **_kwargs: None)

    provider.sync_turn(
        "question",
        "answer",
        session_id="session-1",
        namespace="project-a",
        turn_id=7,
        messages=[
            {"role": "user", "content": "question"},
            {"role": "tool", "name": "web_search", "content": "x", "_tool_output_risk": {"risk": "low"}},
            {"role": "assistant", "content": "answer"},
        ],
    )

    [(path, payload)] = requests
    assert path == "/v1/events/batch"
    assert len(payload["events"]) == 2
    user, assistant = payload["events"]
    assert user["idempotency_key"] == "hermes-turn:session-1:7:user"
    assert assistant["idempotency_key"] == "hermes-turn:session-1:7:assistant"
    assert (user["origin_class"], user["session_kind"]) == ("direct_user", "interactive")
    assert (assistant["origin_class"], assistant["session_kind"]) == ("external_derived", "interactive")
    assert user["metadata"] == {"turn_id": "7"}
    assert assistant["metadata"] == {"turn_id": "7", "external_source_tools": ["web_search"]}
