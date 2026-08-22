from __future__ import annotations

import re
import unicodedata
from typing import Any

from hl_mem.domain.claims.state_projection import state_candidate_key, state_transition_eligible
from hl_mem.domain.claims.temporal_links import SnapshotOrder, TemporalLinkDecision, evaluate_temporal_link
from hl_mem.domain.temporal import parse_utc

STATE_TRANSITION_RULE_VERSION = "state-v1"
_VERSION = re.compile(r"(?<!\d)v?(\d+(?:\.\d+)+)(?!\d)", re.IGNORECASE)


def _state_value(claim: dict[str, Any]) -> str:
    value = unicodedata.normalize("NFKC", str(claim.get("value") or "")).strip().casefold()
    if claim.get("canonical_slot") == "config.version" and (match := _VERSION.search(value)):
        return match.group(1)
    return " ".join(value.split())


def resolve_state_transition(existing: dict[str, Any], new: dict[str, Any]) -> TemporalLinkDecision:
    """Resolve only a proven update on one production ``StateCoordinate``."""

    old_coordinate, new_coordinate = state_candidate_key(existing), state_candidate_key(new)
    if old_coordinate is None or new_coordinate is None or old_coordinate != new_coordinate:
        return TemporalLinkDecision("not_applicable", None, "state_coordinate_differs")
    rule_id = f"{STATE_TRANSITION_RULE_VERSION}:coordinate"
    if not state_transition_eligible(existing) or not state_transition_eligible(new):
        return TemporalLinkDecision("not_applicable", rule_id, "state_not_current_observation")
    if existing.get("_state_group_ambiguous"):
        return TemporalLinkDecision("uncertain", rule_id, "state_group_ambiguous")
    if _state_value(existing) == _state_value(new):
        return TemporalLinkDecision("entails", rule_id, "same_state_value")
    try:
        old_time = parse_utc(existing["valid_from"])
        new_time = parse_utc(new["valid_from"])
    except (KeyError, TypeError, ValueError):
        return TemporalLinkDecision("uncertain", rule_id, "state_time_invalid")
    if old_time == new_time:
        return TemporalLinkDecision("uncertain", rule_id, "state_time_not_strictly_ordered")
    order: SnapshotOrder = "newer" if new_time > old_time else "older"
    return TemporalLinkDecision("snapshot_advance", rule_id, f"{order}_state_observation", order)


def evaluate_state_or_temporal_link(existing: dict[str, Any], new: dict[str, Any]) -> TemporalLinkDecision:
    """Preserve non-state temporal rules while adding the structured state path."""

    if state_candidate_key(existing) is None and state_candidate_key(new) is None:
        return evaluate_temporal_link(existing, new)
    return resolve_state_transition(existing, new)
