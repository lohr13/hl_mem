"""SQLite repository for typed canonical entities, aliases, relations, and claim links."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Literal, cast

from hl_mem.domain.claims.attributes import is_mutually_exclusive_attribute
from hl_mem.domain.claims.conflicts import compute_conflict_key
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
from hl_mem.domain.instruments import InstrumentReference
from hl_mem.lifecycle import assert_transition
from hl_mem.storage._shared import encode_json
from hl_mem.storage.claims import ClaimRepository

AliasSourceKind = Literal["builtin", "config_explicit", "user_explicit", "migration_exact"]

_ALIAS_SOURCE_KINDS = frozenset({"builtin", "config_explicit", "user_explicit", "migration_exact"})
_RELATIONS = frozenset({"runs_on", "owned_by", "operates_in", "part_of", "about"})
_REKEY_IDENTITY_FIELDS = "id namespace_key status subject_entity_id canonical_slot".split()


def v4_entity_conflict_key(claim: Any, canonical_id: str | None) -> str | None:
    qualifiers = claim.get("qualifiers") if isinstance(claim, dict) else json.loads(claim["qualifiers_json"] or "{}")
    return compute_conflict_key(
        str(claim["namespace_key"]),
        str(claim["subject_entity_id"] or ""),
        str(claim["predicate"] or ""),
        claim["canonical_slot"],
        qualifiers,
        version=4,
        subject_canonical_entity_id=canonical_id,
    )


class UnknownCanonicalEntityError(EntityCoordinateError):
    """A persistence operation targeted an alias or nonexistent canonical ID."""


class ActiveAliasConflictError(EntityCoordinateError):
    """The requested alias already has a different active target of the same type."""


class EntityRepository:
    """Persist typed entity coordinates while leaving transactions to the caller."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def cas_rekey_canonical_subject(
        self,
        claims: ClaimRepository,
        expected: dict[str, Any],
        canonical_entity_id: str,
        conflict_key: str | None,
        changed_at: str,
    ) -> str:
        namespace = str(expected["namespace_key"])
        collision = bool(
            expected.get("status") in {"active", "candidate", "disputed"}
            and is_mutually_exclusive_attribute(expected.get("canonical_slot"))
            and conflict_key
            and self.connection.execute(
                "SELECT 1 FROM claims WHERE id<>? AND namespace_key=? AND conflict_key=? "
                "AND status IN ('active','candidate','disputed') LIMIT 1",
                (expected["id"], namespace, conflict_key),
            ).fetchone()
        )
        if collision and expected["status"] != "disputed":
            assert_transition(str(expected["status"]), "disputed")
        status_sql = ",status='disputed'" if collision else ""
        identity = tuple(expected.get(field) for field in _REKEY_IDENTITY_FIELDS)
        cursor = self.connection.execute(
            "UPDATE claims SET subject_canonical_entity_id=?,conflict_key=?,conflict_key_version=4"
            f"{status_sql} WHERE id=? AND namespace_key=? "
            "AND status=? AND subject_entity_id IS ? "
            "AND canonical_slot IS ? AND json(qualifiers_json)=json(?) AND subject_canonical_entity_id IS NULL "
            "AND conflict_key IS ? AND conflict_key_version=?",
            (
                canonical_entity_id,
                conflict_key,
                *identity,
                encode_json(expected.get("qualifiers") or {}, sort_keys=True),
                expected.get("conflict_key"),
                expected.get("conflict_key_version"),
            ),
        )
        if cursor.rowcount == 0:
            return "stale"
        rows = self.connection.execute(
            "SELECT * FROM claims WHERE namespace_key=? AND conflict_key=? AND status IN ('active','candidate','disputed')",
            (namespace, conflict_key),
        ).fetchall()
        members = claims._decode_rows(rows)
        if collision:
            for member in members:
                if member["status"] in {"active", "candidate"}:
                    assert_transition(str(member["status"]), "disputed")
                    claims.update_status(str(member["id"]), "disputed", commit=False)
                    member["status"] = "disputed"
            claims.ensure_group_conflict_case(
                members,
                created_at=changed_at,
                decision="uncertain",
                rationale="entity_rekey_collision",
                commit=False,
            )
            return "quarantined"
        return "updated"

    def _entity_type(self, entity_type: str) -> EntityType:
        if entity_type not in ENTITY_TYPES:
            raise EntityCoordinateError(f"unsupported entity type: {entity_type}")
        return cast(EntityType, entity_type)

    def _canonical_entity(self, entity_id: str, namespace_key: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM canonical_entities WHERE namespace_key=? AND id=?",
            (namespace_key, entity_id),
        ).fetchone()
        if row is None:
            if self.connection.execute("SELECT 1 FROM canonical_entities WHERE id=?", (entity_id,)).fetchone():
                raise EntityTypeMismatchError(f"canonical entity belongs to another namespace: {entity_id}")
            raise UnknownCanonicalEntityError(f"canonical entity does not exist: {namespace_key}/{entity_id}")
        return dict(row)

    def _validate_source_event(self, source_event_id: str | None, namespace_key: str) -> None:
        if source_event_id is None:
            return
        row = self.connection.execute("SELECT tenant_id FROM events WHERE id=?", (source_event_id,)).fetchone()
        if row is None:
            raise EntityCoordinateError(f"source event does not exist: {source_event_id}")
        if row[0] != namespace_key:
            raise EntityTypeMismatchError("source event and entity must share a namespace")

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
        existing = self.connection.execute(
            "SELECT * FROM canonical_entities WHERE namespace_key=? AND id=?",
            (namespace_key, entity_id),
        ).fetchone()
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
        return self._canonical_entity(entity_id, namespace_key)

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
        target = self._canonical_entity(canonical_entity_id, namespace_key)
        if target["entity_type"] != typed:
            raise EntityTypeMismatchError("alias target type or namespace does not match")
        self._validate_source_event(source_event_id, namespace_key)
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
        if row is None:
            raise RuntimeError("entity alias insert was not observable")
        return dict(row)

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
            "JOIN canonical_entities AS entities "
            "ON entities.namespace_key=aliases.namespace_key AND entities.id=aliases.canonical_entity_id "
            "WHERE aliases.namespace_key=? AND aliases.alias_normalized=? "
            "AND aliases.valid_to IS NULL AND entities.status='active' "
            "ORDER BY aliases.entity_type,aliases.canonical_entity_id",
            (namespace_key, normalized),
        ).fetchall()
        candidates: list[ActiveAlias] = []
        for row in rows:
            try:
                validate_canonical_entity_id(str(row[0]), expected_type=str(row[1]))
            except EntityCoordinateError:
                continue
            candidates.append(ActiveAlias(row[0], row[1], row[2]))
        return tuple(candidates)

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

    def resolve_subject_alias(
        self, alias: str, *, namespace_key: str = "default"
    ) -> tuple[str, str, ResolvedEntity | None]:
        try:
            normalized = normalize_typed_alias(alias)
        except EntityCoordinateError:
            return "no_proof", str(alias).strip().casefold(), None
        try:
            resolved = self.resolve_alias(alias, namespace_key=namespace_key, role="subject")
        except EntityCoordinateError:
            return "type_mismatch", normalized, None
        return ("mapping" if resolved else "no_proof"), normalized, resolved

    def instrument_references(
        self, *, namespace_key: str = "default", limit: int = 512
    ) -> tuple[InstrumentReference, ...]:
        """Return bounded active instrument aliases for pure target resolution."""

        if limit < 1:
            raise ValueError("instrument reference limit must be positive")
        rows = self.connection.execute(
            "SELECT entity.id,entity.canonical_key,alias.alias_normalized,alias.version "
            "FROM canonical_entities AS entity JOIN entity_aliases AS alias "
            "ON alias.namespace_key=entity.namespace_key AND alias.canonical_entity_id=entity.id "
            "WHERE entity.namespace_key=? AND entity.entity_type='instrument' AND entity.status='active' "
            "AND alias.entity_type='instrument' AND alias.valid_to IS NULL "
            "ORDER BY entity.id,alias.alias_normalized LIMIT ?",
            (namespace_key, limit + 1),
        ).fetchall()
        if len(rows) > limit:
            return ()
        grouped: dict[tuple[str, str], list[tuple[str, int]]] = {}
        for row in rows:
            grouped.setdefault((str(row[0]), str(row[1])), []).append((str(row[2]), int(row[3])))
        return tuple(
            InstrumentReference(entity_id, canonical_key, tuple(aliases))
            for (entity_id, canonical_key), aliases in grouped.items()
        )

    def seed_builtins(self, namespace_key: str = "default", *, now: str) -> tuple[int, int]:
        """Idempotently seed builtins through the same create and resolve-ready rows."""

        seeds = typed_builtin_seeds()
        before = self.connection.total_changes
        for entity_seed in seeds.entities:
            self.create_entity(
                entity_seed.id,
                entity_seed.entity_type,
                entity_seed.canonical_key,
                entity_seed.display_name,
                namespace_key=namespace_key,
                now=now,
            )
        entity_count = self.connection.total_changes - before
        for alias_seed in seeds.aliases:
            active = self.active_aliases(alias_seed.alias, namespace_key=namespace_key)
            if any(item.entity_type == alias_seed.entity_type for item in active):
                continue
            self.create_alias(
                alias_seed.alias,
                alias_seed.entity_type,
                alias_seed.canonical_entity_id,
                "builtin",
                namespace_key=namespace_key,
                valid_from=now,
            )
        return entity_count, self.connection.total_changes - before - entity_count

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
        self._canonical_entity(from_entity_id, namespace_key)
        self._canonical_entity(to_entity_id, namespace_key)
        self._validate_source_event(source_event_id, namespace_key)
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
        if row is None:
            raise RuntimeError("entity relation insert was not observable")
        return dict(row)

    def link_claim(
        self,
        claim_id: str,
        canonical_entity_id: str,
        role: str,
        *,
        mention_text: str,
        resolution_confidence: float = 1.0,
        alias_version: int | None = None,
        proof_id: str | None = None,
    ) -> dict[str, Any]:
        claim = self.connection.execute("SELECT namespace_key FROM claims WHERE id=?", (claim_id,)).fetchone()
        if claim is None:
            raise EntityCoordinateError(f"claim does not exist: {claim_id}")
        namespace_key = str(claim[0])
        target = self._canonical_entity(canonical_entity_id, namespace_key)
        if target["status"] != "active":
            raise EntityTypeMismatchError("claim entity target must be active")
        validate_entity_role_binding(str(target["entity_type"]), role)
        if alias_version is None or proof_id is None:
            raise EntityTypeMismatchError("claim entity links require alias version and evidence proof")
        normalized_mention = normalize_typed_alias(mention_text)
        active = self.active_aliases(normalized_mention, namespace_key=namespace_key)
        if not any(
            item.canonical_entity_id == canonical_entity_id and item.version == alias_version for item in active
        ):
            raise EntityTypeMismatchError("claim entity alias must be active")
        payload = {
            "claim_id": claim_id,
            "canonical_entity_id": canonical_entity_id,
            "role": role,
            "mention_text": normalized_mention,
            "resolution_confidence": resolution_confidence,
            "alias_version": alias_version,
            "proof_id": proof_id,
        }
        try:
            self.connection.execute(
                "INSERT OR IGNORE INTO claim_entity_links("
                "claim_id,canonical_entity_id,role,mention_text,resolution_confidence,alias_version,proof_id"
                ") VALUES (?,?,?,?,?,?,?)",
                tuple(payload.values()),
            )
        except sqlite3.IntegrityError as exc:
            raise EntityTypeMismatchError("claim entity alias or evidence proof mismatch") from exc
        row = self.connection.execute(
            "SELECT * FROM claim_entity_links WHERE claim_id=? AND canonical_entity_id=? AND role=?",
            (claim_id, canonical_entity_id, role),
        ).fetchone()
        if row is None:
            raise RuntimeError("claim entity link insert was not observable")
        stored = dict(row)
        if stored != payload:
            raise EntityCoordinateError("claim entity link already exists with a different payload")
        return stored
