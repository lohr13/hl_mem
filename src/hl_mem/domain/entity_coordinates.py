"""Pure typed canonical-entity coordinates and alias resolution."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Literal, cast

EntityType = Literal["person", "agent", "device", "environment", "instrument", "project", "topic"]
EntityRole = Literal["subject", "actor", "target", "device", "environment", "project", "about"]

ENTITY_TYPES = frozenset({"person", "agent", "device", "environment", "instrument", "project", "topic"})
ENTITY_ROLES = frozenset({"subject", "actor", "target", "device", "environment", "project", "about"})

_ROLE_ENTITY_TYPES: dict[str, frozenset[str]] = {
    "subject": frozenset({"person", "agent", "device", "environment", "instrument", "project"}),
    "actor": frozenset({"person", "agent"}),
    "target": frozenset({"person", "agent", "device", "environment", "instrument", "project"}),
    "device": frozenset({"device"}),
    "environment": frozenset({"environment"}),
    "project": frozenset({"project"}),
    "about": frozenset({"topic"}),
}

_CONTROLLED_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_EXTERNAL_KEY = re.compile(r"^e_[0-9a-f]{20}$")


class EntityCoordinateError(ValueError):
    """A typed canonical entity coordinate is malformed or unsafe."""


class EntityTypeMismatchError(EntityCoordinateError):
    """An alias or role was requested with an incompatible entity type."""


class AmbiguousEntityAliasError(EntityCoordinateError):
    """An active alias has more than one possible typed interpretation."""


@dataclass(frozen=True, slots=True)
class CanonicalEntitySeed:
    id: str
    entity_type: EntityType
    canonical_key: str
    display_name: str


@dataclass(frozen=True, slots=True)
class EntityAliasSeed:
    alias: str
    entity_type: EntityType
    canonical_entity_id: str


@dataclass(frozen=True, slots=True)
class TypedBuiltinSeeds:
    entities: tuple[CanonicalEntitySeed, ...]
    aliases: tuple[EntityAliasSeed, ...]


@dataclass(frozen=True, slots=True)
class ActiveAlias:
    canonical_entity_id: str
    entity_type: EntityType
    version: int


@dataclass(frozen=True, slots=True)
class ResolvedEntity:
    canonical_entity_id: str
    entity_type: EntityType
    alias_version: int


def _validated_entity_type(entity_type: str) -> EntityType:
    if entity_type not in ENTITY_TYPES:
        raise EntityCoordinateError(f"unsupported entity type: {entity_type}")
    return cast(EntityType, entity_type)


def normalize_typed_alias(value: str) -> str:
    """Normalize only typed aliases, leaving the legacy normalizers unchanged."""

    if not isinstance(value, str):
        raise EntityCoordinateError("entity alias must be a string")
    normalized = " ".join(unicodedata.normalize("NFKC", value).split()).casefold()
    if not normalized:
        raise EntityCoordinateError("entity alias must not be empty")
    return normalized


def _split_entity_id(entity_id: str) -> tuple[EntityType, str]:
    if not isinstance(entity_id, str) or ":" not in entity_id:
        raise EntityCoordinateError(f"canonical entity ID must be typed: {entity_id!r}")
    entity_type, canonical_key = entity_id.split(":", 1)
    return _validated_entity_type(entity_type), canonical_key


def validate_controlled_entity_id(entity_id: str, *, expected_type: str | None = None) -> str:
    """Validate a stable operator-controlled typed ID, excluding generated IDs."""

    entity_type, canonical_key = _split_entity_id(entity_id)
    if expected_type is not None and entity_type != _validated_entity_type(expected_type):
        raise EntityTypeMismatchError(f"expected {expected_type}, got {entity_type}")
    if not _CONTROLLED_KEY.fullmatch(canonical_key) or canonical_key.startswith("e_"):
        raise EntityCoordinateError(f"invalid controlled canonical key: {canonical_key!r}")
    return entity_id


def build_external_entity_id(namespace_key: str, entity_type: str, source_declaration_id: str) -> str:
    """Build a stable ID from immutable external declaration coordinates."""

    typed = _validated_entity_type(entity_type)
    if not namespace_key or not source_declaration_id:
        raise EntityCoordinateError("external entity coordinates must not be empty")
    payload = json.dumps(
        [namespace_key, typed, source_declaration_id],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"{typed}:e_{digest}"


def validate_canonical_entity_id(entity_id: str, *, expected_type: str | None = None) -> str:
    """Validate either a controlled ID or a deterministic generated ID."""

    entity_type, canonical_key = _split_entity_id(entity_id)
    if expected_type is not None and entity_type != _validated_entity_type(expected_type):
        raise EntityTypeMismatchError(f"expected {expected_type}, got {entity_type}")
    if _EXTERNAL_KEY.fullmatch(canonical_key):
        return entity_id
    return validate_controlled_entity_id(entity_id, expected_type=expected_type)


def validate_entity_role_binding(entity_type: str, role: str) -> None:
    """Reject role bindings whose semantics conflict with the canonical type."""

    typed = _validated_entity_type(entity_type)
    if role not in ENTITY_ROLES:
        raise EntityCoordinateError(f"unsupported entity role: {role}")
    if typed not in _ROLE_ENTITY_TYPES[role]:
        raise EntityTypeMismatchError(f"{typed} entities cannot bind to the {role} role")


def resolve_unique_alias(
    mention: str,
    candidates: Iterable[ActiveAlias],
    *,
    expected_type: str | None = None,
    role: str | None = None,
) -> ResolvedEntity | None:
    """Resolve a preselected active alias only when its typed target is unique."""

    normalize_typed_alias(mention)
    if role is not None and role not in ENTITY_ROLES:
        raise EntityCoordinateError(f"unsupported entity role: {role}")
    typed_expected = _validated_entity_type(expected_type) if expected_type is not None else None
    available: list[ActiveAlias] = []
    for candidate in candidates:
        if candidate.version < 1:
            raise EntityCoordinateError("alias version must be positive")
        validate_canonical_entity_id(candidate.canonical_entity_id, expected_type=candidate.entity_type)
        available.append(candidate)
    if typed_expected is not None:
        matching = [candidate for candidate in available if candidate.entity_type == typed_expected]
        if not matching and available:
            observed = ",".join(sorted({candidate.entity_type for candidate in available}))
            raise EntityTypeMismatchError(f"expected {typed_expected}, alias is typed as {observed}")
        available = matching
    if not available:
        return None
    unique = {(candidate.canonical_entity_id, candidate.entity_type, candidate.version) for candidate in available}
    if len(unique) != 1:
        raise AmbiguousEntityAliasError(f"alias is ambiguous across {len(unique)} active targets")
    canonical_entity_id, entity_type, version = next(iter(unique))
    if role is not None:
        validate_entity_role_binding(entity_type, role)
    return ResolvedEntity(canonical_entity_id, entity_type, version)
