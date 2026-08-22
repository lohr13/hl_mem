from typing import Any

from hl_mem.domain.claims.attributes import validate_slot_instance
from hl_mem.domain.claims.conflicts import coordinate_qualifier_key
from hl_mem.domain.claims.state_coordinates import StateCoordinate
from hl_mem.domain.entity import normalize_entity_id
from hl_mem.domain.temporal import canonical_utc_iso

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


def state_valid_from(canonical_slot: str | None, occurred_start: str | None, observed_at: str) -> str:
    """状态使用来源发生时间；其他 Claim 保持事件观察时间。"""
    if canonical_slot in STATE_TRANSITION_SLOTS and occurred_start:
        return canonical_utc_iso(occurred_start)
    return observed_at
