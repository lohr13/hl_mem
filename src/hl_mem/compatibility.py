"""Static compatibility contracts exposed by the daemon and Hermes plugin."""

from __future__ import annotations

from typing import Any

DAEMON_CONTRACT_MAJOR = 1
HERMES_PLUGIN_CONTRACT_MAJOR = 2
CONTEXT_PACKET_SCHEMA_MAJOR = 1
CONTEXT_PACKET_SCHEMA_MINOR = 1


def compatibility_manifest() -> dict[str, Any]:
    """Return the immutable compatibility evidence published by this build."""

    return {
        "daemon_contract_major": DAEMON_CONTRACT_MAJOR,
        "required_plugin_contract_major": HERMES_PLUGIN_CONTRACT_MAJOR,
        "context_packet": {
            "schema_major": CONTEXT_PACKET_SCHEMA_MAJOR,
            "schema_minor": CONTEXT_PACKET_SCHEMA_MINOR,
        },
    }
