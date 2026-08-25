from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from hl_mem.domain.claims.attributes import is_mutually_exclusive_attribute
from hl_mem.domain.entity_coordinates import EntityCoordinateError
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.entities import EntityRepository, v4_entity_conflict_key

_REKEY_FIELDS = "id namespace_key status subject_entity_id canonical_slot conflict_key conflict_key_version subject_canonical_entity_id qualifiers"


@dataclass(frozen=True)
class SubjectResolution:
    outcome: str
    mention: str
    canonical_entity_id: str | None = None
    alias_version: int | None = None


class EntityResolutionService:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.entities = EntityRepository(connection)
        self.claims = ClaimRepository(connection)

    def resolve_subject(self, namespace_key: str, mention: str) -> SubjectResolution:
        outcome, normalized, resolved = self.entities.resolve_subject_alias(mention, namespace_key=namespace_key)
        if resolved:
            return SubjectResolution(outcome, normalized, resolved.canonical_entity_id, resolved.alias_version)
        return SubjectResolution(outcome, normalized)

    def _proof_id(self, claim_id: str) -> str:
        proof = self.connection.execute(
            "SELECT id FROM evidence_links WHERE derived_type='claim' AND derived_id=? "
            "AND relation IN ('derived_from','supports') ORDER BY id LIMIT 1",
            (claim_id,),
        ).fetchone()
        if proof is None:
            raise EntityCoordinateError(f"claim has no persisted evidence proof: {claim_id}")
        return str(proof[0])

    def link_subject(self, claim_id: str, resolution: SubjectResolution) -> None:
        if resolution.canonical_entity_id is None or resolution.alias_version is None:
            return
        self.entities.link_claim(
            claim_id,
            resolution.canonical_entity_id,
            "subject",
            mention_text=resolution.mention,
            alias_version=resolution.alias_version,
            proof_id=self._proof_id(claim_id),
        )

    @staticmethod
    def claim_fingerprint(claim: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(claim.get(field) for field in _REKEY_FIELDS.split())

    def rekey_claim(
        self,
        claim_id: str,
        expected_fingerprint: tuple[Any, ...],
        *,
        changed_at: str,
        commit: bool = True,
    ) -> str:
        try:
            claim = self.claims.get_claim(claim_id)
            if claim is None or self.claim_fingerprint(claim) != expected_fingerprint:
                return "stale"
            resolution = self.resolve_subject(str(claim["namespace_key"]), str(claim["subject_entity_id"] or ""))
            if resolution.canonical_entity_id is None:
                raise EntityCoordinateError(f"claim has no active alias proof: {claim_id}")
            conflict_key = v4_entity_conflict_key(claim, resolution.canonical_entity_id)
            self._proof_id(claim_id)
            outcome = self.claims._cas_rekey_canonical_subject(
                claim, resolution.canonical_entity_id, conflict_key, changed_at
            )
            if outcome != "stale":
                self.link_subject(claim_id, resolution)
            if commit:
                self.connection.commit()
            return outcome
        except Exception:
            if commit and self.connection.in_transaction:
                self.connection.rollback()
            raise

    def rekey_applicable_v3_claims(
        self,
        incoming: dict[str, Any],
        resolution: SubjectResolution,
        *,
        changed_at: str,
    ) -> None:
        slot = incoming.get("canonical_slot")
        if (
            resolution.canonical_entity_id is None
            or incoming.get("conflict_key") is None
            or not is_mutually_exclusive_attribute(slot)
        ):
            return
        rows = self.connection.execute(
            "SELECT DISTINCT claim.id FROM claims AS claim JOIN entity_aliases AS alias "
            "ON alias.namespace_key=claim.namespace_key AND alias.alias_normalized="
            "hl_mem_normalize_alias(claim.subject_entity_id) JOIN canonical_entities AS entity "
            "ON entity.namespace_key=alias.namespace_key AND entity.id=alias.canonical_entity_id "
            "WHERE claim.namespace_key=? AND claim.canonical_slot IS ? AND claim.conflict_key_version<4 "
            "AND claim.subject_canonical_entity_id IS NULL "
            "AND claim.status IN ('active','candidate','disputed') AND alias.valid_to IS NULL "
            "AND alias.source_kind IN ('builtin','config_explicit','user_explicit','migration_exact') "
            "AND alias.canonical_entity_id=? AND entity.status='active' ORDER BY claim.id LIMIT ?",
            (incoming["namespace_key"], incoming.get("canonical_slot"), resolution.canonical_entity_id, 17),
        ).fetchall()
        if len(rows) > 16:
            raise EntityCoordinateError("applicable legacy entity rekey overflow")
        for row in rows:
            claim = self.claims.get_claim(str(row[0]))
            if claim is None:
                raise EntityCoordinateError("legacy entity rekey disappeared")
            if v4_entity_conflict_key(claim, resolution.canonical_entity_id) != incoming.get("conflict_key"):
                continue
            if (
                self.rekey_claim(str(row[0]), self.claim_fingerprint(claim), changed_at=changed_at, commit=False)
                == "stale"
            ):
                raise EntityCoordinateError("legacy entity rekey became stale")
