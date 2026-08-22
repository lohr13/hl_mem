from typing import Any

from hl_mem.domain.claims.attributes import validate_slot_instance
from hl_mem.domain.claims.conflicts import coordinate_qualifier_key
from hl_mem.domain.claims.state_coordinates import StateCoordinate
from hl_mem.domain.entity import normalize_entity_id

STATE_TRANSITION_SLOTS = frozenset(
    "config.version state.service_health state.process state.deployment state.connectivity state.job".split()
)


def project_state_coordinate(
    *, namespace: str, subject: str, canonical_slot: str | None, qualifiers: dict[str, Any] | None
) -> StateCoordinate | None:
    """Fail closed 地投影已注册且主体可信的状态轴。"""
    values = qualifiers or {}
    slot = validate_slot_instance(canonical_slot, values)
    owner = normalize_entity_id(subject)
    if slot not in STATE_TRANSITION_SLOTS or owner == "unknown":
        return None
    return StateCoordinate(namespace, owner, slot, coordinate_qualifier_key(slot, values))
