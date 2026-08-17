"""Deterministic, feature-gated resurrection of archived claims."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from typing import Any, Callable

from hl_mem.domain.temporal import RecallIntent, claim_is_visible
from hl_mem.lifecycle import assert_transition
from hl_mem.observability.audit import current_audit
from hl_mem.protocols import EmbedderProtocol
from hl_mem.recall.lexicalizer import tokenize_for_fts
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository

LOGGER = logging.getLogger(__name__)


class ResurrectionService:
    """Resurrect at most one safe, high-overlap archived FTS candidate."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        embedder: EmbedderProtocol,
        settings: Settings | None = None,
        defer_activation: Callable[[str, bytes, str, int, str, str, str | None], bool] | None = None,
    ) -> None:
        self.connection = connection
        self.embedder = embedder
        self.settings = settings or Settings()
        self.repository = ClaimRepository(connection, settings=self.settings)
        self.defer_activation = defer_activation

    def try_resurrect(
        self,
        query: str,
        *,
        namespace: str,
        as_of: str,
        known_as_of: str | None = None,
        intent: RecallIntent | str = RecallIntent.CURRENT_STATE,
    ) -> dict[str, Any] | None:
        """Return the activated claim, or ``None`` when any safety gate fails."""

        selected_intent = RecallIntent(intent)
        if (
            self.settings.resurrection_mode != "auto"
            or selected_intent is RecallIntent.HISTORICAL
            or known_as_of is not None
        ):
            return None
        query_terms = {term.casefold() for term in tokenize_for_fts(query, language=self.settings.fts_language)}
        if not query_terms:
            return None
        candidates = self.repository.search_archived_claims_fts(
            query,
            self.settings.resurrection_candidate_limit,
            as_of,
            known_as_of,
            namespace,
        )
        for candidate in candidates:
            document_terms = {
                term.casefold()
                for term in tokenize_for_fts(
                    str(candidate.get("index_text") or ""),
                    language=self.settings.fts_language,
                )
            }
            coverage = len(query_terms & document_terms) / len(query_terms)
            if coverage < self.settings.resurrection_min_term_coverage:
                continue
            if not self._source_is_complete(str(candidate["id"])):
                continue
            if self._has_active_rival(candidate):
                continue
            embedding_text = str(candidate.get("index_text") or "").strip()
            if not embedding_text:
                continue
            try:
                embedding = self.embedder.embed_one(embedding_text)
            except RuntimeError as error:
                LOGGER.warning(
                    "resurrection embedding failed; preserving original recall result: %s",
                    type(error).__name__,
                )
                current_audit().emit(
                    "recall",
                    "resurrection",
                    "embedding_error_fallback",
                    claim_id=str(candidate["id"]),
                    detail={"error_class": type(error).__name__},
                )
                continue
            if self.defer_activation is not None:
                projected: dict[str, Any] = {
                    **candidate,
                    "status": "active",
                    "embedding_dense": embedding,
                    "embedding_model": self.embedder.model,
                    "embedding_dim": self.embedder.dim,
                }
                if not claim_is_visible(projected, as_of, known_as_of, RecallIntent.CURRENT_STATE):
                    continue
                accepted = self.defer_activation(
                    str(candidate["id"]),
                    embedding,
                    self.embedder.model,
                    self.embedder.dim,
                    namespace,
                    as_of,
                    known_as_of,
                )
                activated = projected if accepted else None
            else:
                activated = self._activate(
                    str(candidate["id"]),
                    embedding,
                    namespace=namespace,
                    as_of=as_of,
                    known_as_of=known_as_of,
                )
            if activated is None:
                continue
            try:
                current_audit().emit(
                    "recall",
                    "resurrection",
                    "resurrected",
                    claim_id=str(activated["id"]),
                    detail={"confidence_changed": False, "source": "archived_fts"},
                )
            except Exception:
                LOGGER.exception("resurrection audit emission failed")
            return activated
        return None

    def _activate(
        self,
        claim_id: str,
        embedding: bytes,
        *,
        namespace: str,
        as_of: str,
        known_as_of: str | None,
    ) -> dict[str, Any] | None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            claim = self.repository.get_claim(claim_id)
            if claim is None or claim.get("status") != "archived" or claim.get("namespace_key") != namespace:
                self.connection.rollback()
                return None
            projected: Mapping[str, Any] = {**claim, "status": "active"}
            if not claim_is_visible(projected, as_of, known_as_of, RecallIntent.CURRENT_STATE):
                self.connection.rollback()
                return None
            if not self._source_is_complete(claim_id) or self._has_active_rival(claim):
                self.connection.rollback()
                return None
            assert_transition("archived", "active")
            cursor = self.connection.execute(
                "UPDATE claims SET status='active',embedding_dense=?,embedding_sparse=NULL,"
                "embedding_model=?,embedding_dim=? WHERE id=? AND status='archived'",
                (embedding, self.embedder.model, self.embedder.dim, claim_id),
            )
            if cursor.rowcount != 1:
                self.connection.rollback()
                return None
            self.repository.sync_vector(claim_id)
            self.connection.commit()
        except sqlite3.IntegrityError as error:
            self.connection.rollback()
            if "exclusive conflict group" in str(error):
                return None
            raise
        except Exception:
            self.connection.rollback()
            raise
        return self.repository.get_claim(claim_id)

    def _source_is_complete(self, claim_id: str) -> bool:
        rows = self.connection.execute(
            "SELECT e.evidence_type,source_event.id AS event_id,"
            "source_claim.id AS claim_id,source_claim.status AS claim_status FROM evidence_links e "
            "LEFT JOIN events source_event ON e.evidence_type='event' AND source_event.id=e.evidence_id "
            "LEFT JOIN claims source_claim ON e.evidence_type='claim' AND source_claim.id=e.evidence_id "
            "WHERE e.derived_type='claim' AND e.derived_id=?",
            (claim_id,),
        ).fetchall()
        if not rows:
            return False
        return all(
            (row["evidence_type"] == "event" and row["event_id"] is not None)
            or (
                row["evidence_type"] == "claim"
                and row["claim_id"] is not None
                and row["claim_status"] not in {"candidate", "retracted"}
            )
            for row in rows
        )

    def _has_active_rival(self, claim: Mapping[str, Any]) -> bool:
        conflict_key = claim.get("conflict_key")
        if not conflict_key:
            return False
        return (
            self.connection.execute(
                "SELECT 1 FROM claims WHERE namespace_key=? AND conflict_key=? "
                "AND status='active' AND id<>? LIMIT 1",
                (claim.get("namespace_key"), conflict_key, claim.get("id")),
            ).fetchone()
            is not None
        )
