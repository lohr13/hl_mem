"""v0.29.0 static daemon/plugin/wire compatibility contracts."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from hl_mem.adapters.hermes.deployment import PLUGIN_SOURCE_DIR
from hl_mem.api.schemas import ContextPacketOutput, EventInput
from hl_mem.api.server import create_app
from hl_mem.compatibility import (
    CONTEXT_PACKET_SCHEMA_MAJOR,
    CONTEXT_PACKET_SCHEMA_MINOR,
    DAEMON_CONTRACT_MAJOR,
    HERMES_PLUGIN_CONTRACT_MAJOR,
    compatibility_manifest,
)


def test_healthz_publishes_static_compatibility_manifest(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "compatibility.db")) as client:
        body = client.get("/healthz").json()

    assert body["compatibility"] == compatibility_manifest()


def test_packaged_plugin_contract_matches_daemon_and_wire_constants() -> None:
    contract = json.loads((PLUGIN_SOURCE_DIR / "contract.json").read_text(encoding="utf-8"))

    assert contract == {
        "plugin_contract_major": HERMES_PLUGIN_CONTRACT_MAJOR,
        "daemon_contract_major": DAEMON_CONTRACT_MAJOR,
        "context_packet_schema_major": CONTEXT_PACKET_SCHEMA_MAJOR,
        "context_packet_schema_minor": CONTEXT_PACKET_SCHEMA_MINOR,
    }


def test_legacy_event_and_context_packet_contracts_accept_provenance_additions() -> None:
    legacy_event = EventInput(content="legacy")
    assert (legacy_event.origin_class, legacy_event.session_kind) == ("unknown", "unknown")

    packet = ContextPacketOutput.model_validate(
        {
            "schema_major": CONTEXT_PACKET_SCHEMA_MAJOR,
            "schema_minor": CONTEXT_PACKET_SCHEMA_MINOR,
            "query_id": "query-1",
            "answerability": "supported",
            "feedback_state": "available",
            "items": [
                {
                    "type": "claim",
                    "id": "claim-1",
                    "text": "public memory",
                    "evidence": [
                        {
                            "type": "event",
                            "id": "event-1",
                            "provenance": {
                                "origin_class": "external",
                                "session_kind": "interactive",
                            },
                        }
                    ],
                    "feedback_id": "feedback-1",
                }
            ],
            "used_tokens_estimate": 7,
            "truncated": False,
        }
    )
    assert packet.items[0].evidence[0]["provenance"]["origin_class"] == "external"
