"""SQLite repository for typed canonical entities, aliases, relations, and claim links."""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any, Literal, cast

from hl_mem.domain.entity import typed_builtin_seeds
from hl_mem.domain.entity_coordinates import (
    ENTITY_TYPES,
    ActiveAlias,
    EntityCoordinateError,
    EntityType,
    EntityTypeMismatchError,
    ResolvedEntity,
    normalize_typed_alias,
    resolve_unique_alias,
    validate_canonical_entity_id,
    validate_entity_role_binding,
)

AliasSourceKind = Literal["builtin", "config_explicit", "user_explicit", "migration_exact"]

_ALIAS_SOURCE_KINDS = frozenset({"builtin", "config_explicit", "user_explicit", "migration_exact"})
_RELATIONS = frozenset({"runs_on", "owned_by", "operates_in", "part_of", "about"})


class UnknownCanonicalEntityError(EntityCoordinateError):
    """A persistence operation targeted an alias or nonexistent canonical ID."""


class ActiveAliasConflictError(EntityCoordinateError):
    """The requested alias already has a different active target of the same type."""


class EntityRepository:
    """Persist typed entity coordinates while leaving transactions to the caller."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    @staticmethod
    def _entity_type(entity_type: str) -> EntityType:
        if entity_type not in ENTITY_TYPES:
            raise EntityCoordinateError(f"unsupported entity type: {entity_type}")
        return cast(EntityType, entity_type)

    def _canonical_entity(self, entity_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM canonical_entities WHERE id=?", (entity_id,)).fetchone()
        if row is None:
            raise UnknownCanonicalEntityError(f"canonical entity does not exist: {entity_id}")
        return dict(row)

    def create_entity(
        self,
        entity_id: str,
        entity_type: str,
        canonical_key: str,
        display_name: str,
        *,
        namespace_key: str = "default",
        status: str = "active",
        now: str,
    ) -> dict[str, Any]:
        """Create one stable canonical coordinate, or return its identical existing row."""

        typed = self._entity_type(entity_type)
        validate_canonical_entity_id(entity_id, expected_type=typed)
        if entity_id != f"{typed}:{canonical_key}":
            raise EntityCoordinateError("canonical entity ID and canonical key disagree")
        existing = self.connection.execute("SELECT * FROM canonical_entities WHERE id=?", (entity_id,)).fetchone()
        if existing is not None:
            decoded = dict(existing)
            coordinate = (decoded["namespace_key"], decoded["entity_type"], decoded["canonical_key"])
            if coordinate != (namespace_key, typed, canonical_key):
                raise EntityCoordinateError(f"canonical entity ID is already bound to {coordinate}")
            return decoded
        self.connection.execute(
            "INSERT INTO canonical_entities("
            "id,namespace_key,entity_type,canonical_key,display_name,status,created_at,updated_at"
            ") VALUES (?,?,?,?,?,?,?,?)",
            (entity_id, namespace_key, typed, canonical_key, display_name, status, now, now),
        )
        return self._canonical_entity(entity_id)

    def create_alias(
        self,
        alias: str,
        entity_type: str,
        canonical_entity_id: str,
        source_kind: AliasSourceKind,
        *,
        namespace_key: str = "default",
        source_event_id: str | None = None,
        valid_from: str,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        """Create the next alias version, never an alias-to-alias edge."""

        typed = self._entity_type(entity_type)
        if source_kind not in _ALIAS_SOURCE_KINDS:
            raise EntityCoordinateError(f"unsupported alias source kind: {source_kind}")
        target = self._canonical_entity(canonical_entity_id)
        if target["entity_type"] != typed or target["namespace_key"] != namespace_key:
            raise EntityTypeMismatchError("alias target type or namespace does not match")
        normalized = normalize_typed_alias(alias)
        active = self.connection.execute(
            "SELECT * FROM entity_aliases WHERE namespace_key=? AND alias_normalized=? "
            "AND entity_type=? AND valid_to IS NULL",
            (namespace_key, normalized, typed),
        ).fetchone()
        if active is not None:
            decoded = dict(active)
            if decoded["canonical_entity_id"] == canonical_entity_id:
                return decoded
            raise ActiveAliasConflictError(f"active {typed} alias already targets {decoded['canonical_entity_id']}")
        previous = self.connection.execute(
            "SELECT COALESCE(MAX(version),0) FROM entity_aliases "
            "WHERE namespace_key=? AND alias_normalized=? AND entity_type=?",
            (namespace_key, normalized, typed),
        ).fetchone()[0]
        alias_id = uuid.uuid4().hex
        self.connection.execute(
            "INSERT INTO entity_aliases("
            "id,namespace_key,alias_normalized,entity_type,canonical_entity_id,version,source_kind,"
            "source_event_id,valid_from,valid_to,created_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,NULL,?)",
            (
                alias_id,
                namespace_key,
                normalized,
                typed,
                canonical_entity_id,
                int(previous) + 1,
                source_kind,
                source_event_id,
                valid_from,
                created_at or valid_from,
            ),
        )
        row = self.connection.execute("SELECT * FROM entity_aliases WHERE id=?", (alias_id,)).fetchone()
        return cast(dict[str, Any], self._row(row))

    def close_alias(
        self,
        alias: str,
        entity_type: str,
        *,
        namespace_key: str = "default",
        valid_to: str,
    ) -> int:
        """Close the sole active typed alias without rewriting its historical version."""

        typed = self._entity_type(entity_type)
        normalized = normalize_typed_alias(alias)
        cursor = self.connection.execute(
            "UPDATE entity_aliases SET valid_to=? WHERE namespace_key=? AND alias_normalized=? "
            "AND entity_type=? AND valid_to IS NULL",
            (valid_to, namespace_key, normalized, typed),
        )
        return cursor.rowcount

    def active_aliases(self, alias: str, *, namespace_key: str = "default") -> tuple[ActiveAlias, ...]:
        """Return active targets across types; ambiguity is resolved by the pure resolver."""

        normalized = normalize_typed_alias(alias)
        rows = self.connection.execute(
            "SELECT aliases.canonical_entity_id,aliases.entity_type,aliases.version "
            "FROM entity_aliases AS aliases "
            "JOIN canonical_entities AS entities ON entities.id=aliases.canonical_entity_id "
            "WHERE aliases.namespace_key=? AND aliases.alias_normalized=? "
            "AND aliases.valid_to IS NULL AND entities.status='active' "
            "ORDER BY aliases.entity_type,aliases.canonical_entity_id",
            (namespace_key, normalized),
        ).fetchall()
        return tuple(ActiveAlias(row[0], row[1], row[2]) for row in rows)

    def resolve_alias(
        self,
        alias: str,
        *,
        namespace_key: str = "default",
        expected_type: str | None = None,
        role: str | None = None,
    ) -> ResolvedEntity | None:
        return resolve_unique_alias(
            alias,
            self.active_aliases(alias, namespace_key=namespace_key),
            expected_type=expected_type,
            role=role,
        )

    def seed_builtins(self, namespace_key: str = "default", *, now: str) -> tuple[int, int]:
        """Idempotently seed builtins through the same create and resolve-ready rows."""

        seeds = typed_builtin_seeds()
        entity_count = 0
        alias_count = 0
        for entity_seed in seeds.entities:
            existed = self.connection.execute(
                "SELECT 1 FROM canonical_entities WHERE id=?", (entity_seed.id,)
            ).fetchone()
            self.create_entity(
                entity_seed.id,
                entity_seed.entity_type,
                entity_seed.canonical_key,
                entity_seed.display_name,
                namespace_key=namespace_key,
                now=now,
            )
            entity_count += existed is None
        for alias_seed in seeds.aliases:
            normalized = normalize_typed_alias(alias_seed.alias)
            existed = self.connection.execute(
                "SELECT 1 FROM entity_aliases WHERE namespace_key=? AND alias_normalized=? "
                "AND entity_type=? AND valid_to IS NULL",
                (namespace_key, normalized, alias_seed.entity_type),
            ).fetchone()
            self.create_alias(
                alias_seed.alias,
                alias_seed.entity_type,
                alias_seed.canonical_entity_id,
                "builtin",
                namespace_key=namespace_key,
                valid_from=now,
            )
            alias_count += existed is None
        return entity_count, alias_count

    def create_relation(
        self,
        from_entity_id: str,
        to_entity_id: str,
        relation: str,
        *,
        confidence: float,
        valid_from: str,
        namespace_key: str = "default",
        source_event_id: str | None = None,
        valid_to: str | None = None,
    ) -> dict[str, Any]:
        if relation not in _RELATIONS:
            raise EntityCoordinateError(f"unsupported entity relation: {relation}")
        source = self._canonical_entity(from_entity_id)
        target = self._canonical_entity(to_entity_id)
        if source["namespace_key"] != namespace_key or target["namespace_key"] != namespace_key:
            raise EntityTypeMismatchError("relation endpoints must share the declared namespace")
        relation_id = uuid.uuid4().hex
        self.connection.execute(
            "INSERT INTO entity_relations("
            "id,namespace_key,from_entity_id,to_entity_id,relation,source_event_id,confidence,valid_from,valid_to"
            ") VALUES (?,?,?,?,?,?,?,?,?)",
            (
                relation_id,
                namespace_key,
                from_entity_id,
                to_entity_id,
                relation,
                source_event_id,
                confidence,
                valid_from,
                valid_to,
            ),
        )
        row = self.connection.execute("SELECT * FROM entity_relations WHERE id=?", (relation_id,)).fetchone()
        return cast(dict[str, Any], self._row(row))

    def link_claim(
        self,
        claim_id: str,
        canonical_entity_id: str,
        role: str,
        *,
        mention_text: str,
        resolution_confidence: float,
        alias_version: int | None = None,
        proof_id: str | None = None,
    ) -> dict[str, Any]:
        target = self._canonical_entity(canonical_entity_id)
        validate_entity_role_binding(str(target["entity_type"]), role)
        claim = self.connection.execute("SELECT namespace_key FROM claims WHERE id=?", (claim_id,)).fetchone()
        if claim is not None and claim[0] != target["namespace_key"]:
            raise EntityTypeMismatchError("claim and canonical entity must share a namespace")
        self.connection.execute(
            "INSERT OR IGNORE INTO claim_entity_links("
            "claim_id,canonical_entity_id,role,mention_text,resolution_confidence,alias_version,proof_id"
            ") VALUES (?,?,?,?,?,?,?)",
            (
                claim_id,
                canonical_entity_id,
                role,
                mention_text,
                resolution_confidence,
                alias_version,
                proof_id,
            ),
        )
        row = self.connection.execute(
            "SELECT * FROM claim_entity_links WHERE claim_id=? AND canonical_entity_id=? AND role=?",
            (claim_id, canonical_entity_id, role),
        ).fetchone()
        if row is None:
            raise RuntimeError("claim entity link insert was not observable")
        return dict(row)
