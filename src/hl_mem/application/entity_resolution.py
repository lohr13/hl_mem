"""Conservative typed-entity projection for Claim writes and backfill audits."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from hl_mem.domain.entity_coordinates import (
    AmbiguousEntityAliasError,
    EntityCoordinateError,
    normalize_typed_alias,
)
from hl_mem.storage.entities import EntityRepository


@dataclass(frozen=True)
class SubjectResolution:
    outcome: str
    mention: str
    canonical_entity_id: str | None = None
    alias_version: int | None = None


class EntityResolutionService:
    """Resolve and link only active, source-proven, pre-existing aliases."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.entities = EntityRepository(connection)

    def resolve_subject(self, namespace_key: str, mention: str) -> SubjectResolution:
        try:
            normalized = normalize_typed_alias(mention)
        except EntityCoordinateError:
            return SubjectResolution("no_proof", str(mention).strip().casefold())
        try:
            resolved = self.entities.resolve_alias(mention, namespace_key=namespace_key, role="subject")
        except (AmbiguousEntityAliasError, EntityCoordinateError):
            return SubjectResolution("type_mismatch", normalized)
        if resolved is None:
            return SubjectResolution("no_proof", normalized)
        proof = self.connection.execute(
            "SELECT version FROM entity_aliases WHERE namespace_key=? AND alias_normalized=? "
            "AND entity_type=? AND canonical_entity_id=? AND valid_to IS NULL "
            "AND source_kind IN ('builtin','config_explicit','user_explicit','migration_exact')",
            (namespace_key, normalized, resolved.entity_type, resolved.canonical_entity_id),
        ).fetchone()
        if proof is None:
            return SubjectResolution("no_proof", normalized)
        return SubjectResolution("mapping", normalized, resolved.canonical_entity_id, int(proof[0]))

    def link_subject(self, claim_id: str, resolution: SubjectResolution) -> None:
        if resolution.canonical_entity_id is None or resolution.alias_version is None:
            return
        proof = self.connection.execute(
            "SELECT id FROM evidence_links WHERE derived_type='claim' AND derived_id=? "
            "AND relation IN ('derived_from','supports') ORDER BY id LIMIT 1",
            (claim_id,),
        ).fetchone()
        if proof is None:
            raise EntityCoordinateError(f"claim has no persisted evidence proof: {claim_id}")
        self.entities.link_claim(
            claim_id,
            resolution.canonical_entity_id,
            "subject",
            mention_text=resolution.mention,
            resolution_confidence=1.0,
            alias_version=resolution.alias_version,
            proof_id=str(proof[0]),
        )
