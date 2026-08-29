"""Fail-closed physical deletion closure backed by an independent tombstone ledger."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hl_mem.application.conflict_queries import OPEN_CASE_STATUSES
from hl_mem.errors import ConflictError, NotFoundError
from hl_mem.observability.audit import audit_scope
from hl_mem.recall.recall_pipeline import stale_observations
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.tombstones import (
    TOMBSTONE_SCHEMA_VERSION,
    TombstoneEntry,
    TombstoneLedger,
    TombstoneLedgerError,
    TombstoneLedgerWriteError,
    default_tombstone_ledger_path,
)

DELETABLE_STATUSES = frozenset({"active", "archived", "superseded"})
REPLAYABLE_STATUS = "retracted"
DELETION_CLOSURE_SCOPE = (
    "claim",
    "exclusive_evidence",
    "relations",
    "conflicts",
    "derivation_references",
    "unreferenced_events",
)


class DeletionRejectedError(ConflictError, ValueError):
    """Deletion cannot prove that the requested closure is safe."""

    def __init__(self, claim_id: str, reason: str, detail: str | None = None) -> None:
        self.claim_id = claim_id
        self.reason = reason
        suffix = f": {detail}" if detail else ""
        super().__init__(f"deletion rejected for {claim_id}: {reason}{suffix}")


@dataclass(frozen=True)
class DeletionResult:
    claim_id: str
    deleted: bool
    already_deleted: bool
    identity_hash: str
    deleted_event_ids: tuple[str, ...]


@dataclass(frozen=True)
class ArchivedCleanupReport:
    scanned: int
    deleted: int
    rejected: int
    rejections: dict[str, str]


@dataclass(frozen=True)
class TombstoneReplayResult:
    identity_hash: str
    claims_removed: int
    events_removed: int


class DeletionService:
    """Own the single physical-deletion transaction used by every entry point."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        ledger_path: str | Path | None = None,
    ) -> None:
        self.connection = connection
        self.ledger_path = Path(ledger_path).expanduser().resolve() if ledger_path else None

    def delete_claim(self, claim_id: str) -> DeletionResult:
        with audit_scope(claim_mutation_source="delete_claim"):
            return self._delete_claim(claim_id, allow_expired=False, require_no_evidence_consumers=False)

    def delete_expired_claim(self, claim_id: str) -> DeletionResult:
        """Delete one expired Claim only after proving it has no downstream evidence consumers."""
        with audit_scope(claim_mutation_source="delete_expired_claim"):
            return self._delete_claim(claim_id, allow_expired=True, require_no_evidence_consumers=True)

    def _delete_claim(
        self,
        claim_id: str,
        *,
        allow_expired: bool,
        require_no_evidence_consumers: bool,
    ) -> DeletionResult:
        normalized_id = str(claim_id).strip()
        if not normalized_id:
            raise NotFoundError("memory not found: ")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            claim = self.connection.execute(
                "SELECT id,status FROM claims WHERE id=?",
                (normalized_id,),
            ).fetchone()
            if claim is None:
                result = self._idempotent_replay(normalized_id)
                self.connection.commit()
                return result

            self._assert_deletable(
                normalized_id,
                str(claim["status"]),
                allow_expired=allow_expired,
            )
            if require_no_evidence_consumers:
                consumer = self.connection.execute(
                    "SELECT id,derived_type,derived_id FROM evidence_links "
                    "WHERE evidence_type='claim' AND evidence_id=? ORDER BY id LIMIT 1",
                    (normalized_id,),
                ).fetchone()
                if consumer is not None:
                    raise DeletionRejectedError(
                        normalized_id,
                        "evidence_consumers",
                        f"{consumer['id']}:{consumer['derived_type']}:{consumer['derived_id']}",
                    )
            event_ids = self._exclusive_event_ids(normalized_id)
            ledger = self._bound_ledger(normalized_id)
            try:
                tombstone = ledger.record_deletion(
                    claim_ids=(normalized_id,),
                    event_ids=event_ids,
                    closure_scope=DELETION_CLOSURE_SCOPE,
                )
            except TombstoneLedgerWriteError as error:
                raise DeletionRejectedError(
                    normalized_id,
                    "ledger_write_failed",
                    str(error),
                ) from error
            except TombstoneLedgerError as error:
                raise DeletionRejectedError(
                    normalized_id,
                    "ledger_invalid",
                    str(error),
                ) from error

            self._delete_closure(normalized_id, event_ids)
            applied_at = datetime.now(timezone.utc).isoformat()
            self.connection.execute(
                "UPDATE deletion_ledger_state SET last_identity_hash=?,last_applied_at=? " "WHERE singleton=1",
                (tombstone.identity_hash, applied_at),
            )
            self.connection.commit()
            return DeletionResult(
                claim_id=normalized_id,
                deleted=True,
                already_deleted=False,
                identity_hash=tombstone.identity_hash,
                deleted_event_ids=event_ids,
            )
        except Exception:
            if self.connection.in_transaction:
                self.connection.rollback()
            raise

    def cleanup_archived(
        self,
        *,
        limit: int = 100,
        namespace: str | None = None,
    ) -> ArchivedCleanupReport:
        """Bounded archived cleanup; each item reuses ``delete_claim`` unchanged."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        query = "SELECT id FROM claims WHERE status='archived'"
        parameters: list[Any] = []
        if namespace is not None:
            query += " AND namespace_key=?"
            parameters.append(namespace)
        query += " ORDER BY recorded_from,id LIMIT ?"
        parameters.append(limit)
        claim_ids = [str(row[0]) for row in self.connection.execute(query, parameters).fetchall()]
        deleted = 0
        rejections: dict[str, str] = {}
        for archived_claim_id in claim_ids:
            try:
                with audit_scope(claim_mutation_source="cleanup_archived"):
                    self._delete_claim(
                        archived_claim_id,
                        allow_expired=False,
                        require_no_evidence_consumers=False,
                    )
                deleted += 1
            except DeletionRejectedError as error:
                rejections[archived_claim_id] = error.reason
        return ArchivedCleanupReport(
            scanned=len(claim_ids),
            deleted=deleted,
            rejected=len(rejections),
            rejections=rejections,
        )

    def replay_tombstone(self, entry: TombstoneEntry) -> TombstoneReplayResult:
        """Apply one authoritative ledger entry without re-adjudicating old claim state."""
        error_id = entry.claim_ids[0] if entry.claim_ids else entry.identity_hash
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            ledger = self._bound_ledger(error_id)
            recorded = ledger.find_by_identity_hash(entry.identity_hash)
            if recorded != entry:
                raise DeletionRejectedError(error_id, "ledger_entry_mismatch")

            claims_removed = 0
            for claim_id in entry.claim_ids:
                exists = self.connection.execute(
                    "SELECT 1 FROM claims WHERE id=?",
                    (claim_id,),
                ).fetchone()
                if exists is None:
                    continue
                with audit_scope(claim_mutation_source="tombstone_replay"):
                    self._delete_closure(claim_id, ())
                claims_removed += 1

            events_removed = 0
            for event_id in entry.event_ids:
                cursor = self.connection.execute(
                    "DELETE FROM events WHERE id=? "
                    "AND NOT EXISTS ("
                    "SELECT 1 FROM evidence_links "
                    "WHERE evidence_type='event' AND evidence_id=?"
                    ") AND NOT EXISTS ("
                    "SELECT 1 FROM deferred_tasks "
                    "WHERE resource_type='event' AND resource_id=? AND status='pending'"
                    ")",
                    (event_id, event_id, event_id),
                )
                events_removed += cursor.rowcount

            self.connection.execute(
                "UPDATE deletion_ledger_state SET last_identity_hash=?,last_applied_at=? " "WHERE singleton=1",
                (entry.identity_hash, datetime.now(timezone.utc).isoformat()),
            )
            self.connection.commit()
            return TombstoneReplayResult(
                identity_hash=entry.identity_hash,
                claims_removed=claims_removed,
                events_removed=events_removed,
            )
        except Exception:
            if self.connection.in_transaction:
                self.connection.rollback()
            raise

    def _assert_deletable(self, claim_id: str, status: str, *, allow_expired: bool = False) -> None:
        allowed_statuses = DELETABLE_STATUSES | ({"expired"} if allow_expired else set())
        if status not in allowed_statuses and status != REPLAYABLE_STATUS:
            raise DeletionRejectedError(claim_id, f"status_{status}")
        placeholders = ",".join("?" for _ in OPEN_CASE_STATUSES)
        open_case = self.connection.execute(
            "SELECT id,status FROM conflict_cases "
            "WHERE (left_claim_id=? OR right_claim_id=?) "
            f"AND status IN ({placeholders}) ORDER BY created_at,id LIMIT 1",
            (claim_id, claim_id, *OPEN_CASE_STATUSES),
        ).fetchone()
        if open_case is not None:
            raise DeletionRejectedError(
                claim_id,
                "open_conflict_case",
                f"{open_case['id']}:{open_case['status']}",
            )

    def _exclusive_event_ids(self, claim_id: str) -> tuple[str, ...]:
        rows = self.connection.execute(
            "SELECT DISTINCT link.evidence_id FROM evidence_links AS link "
            "WHERE link.derived_type='claim' AND link.derived_id=? "
            "AND link.evidence_type='event' "
            "AND NOT EXISTS ("
            "SELECT 1 FROM evidence_links AS other "
            "WHERE other.evidence_type='event' AND other.evidence_id=link.evidence_id "
            "AND NOT (other.derived_type='claim' AND other.derived_id=?)"
            ") AND NOT EXISTS ("
            "SELECT 1 FROM deferred_tasks AS task "
            "WHERE task.resource_type='event' AND task.resource_id=link.evidence_id "
            "AND task.status='pending'"
            ") ORDER BY link.evidence_id",
            (claim_id, claim_id),
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def _bound_ledger(self, claim_id: str) -> TombstoneLedger:
        path = self.ledger_path or self._derived_ledger_path(claim_id)
        state = self.connection.execute(
            "SELECT ledger_id,schema_version FROM deletion_ledger_state WHERE singleton=1"
        ).fetchone()
        if state is not None and not path.is_file():
            raise DeletionRejectedError(claim_id, "ledger_missing", str(path))
        try:
            ledger = TombstoneLedger(path, create=state is None)
            ledger.validate()
        except (OSError, TombstoneLedgerError) as error:
            reason = "ledger_missing" if isinstance(error, FileNotFoundError) else "ledger_invalid"
            raise DeletionRejectedError(claim_id, reason, str(error)) from error
        if state is None:
            self.connection.execute(
                "INSERT INTO deletion_ledger_state(" "singleton,ledger_id,schema_version,bound_at" ") VALUES (1,?,?,?)",
                (
                    ledger.ledger_id,
                    TOMBSTONE_SCHEMA_VERSION,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            return ledger
        if int(state["schema_version"]) != TOMBSTONE_SCHEMA_VERSION:
            raise DeletionRejectedError(claim_id, "ledger_version_mismatch")
        if str(state["ledger_id"]) != ledger.ledger_id:
            raise DeletionRejectedError(claim_id, "ledger_identity_mismatch")
        return ledger

    def _derived_ledger_path(self, claim_id: str) -> Path:
        rows = self.connection.execute("PRAGMA database_list").fetchall()
        database_path = next((str(row[2]) for row in rows if str(row[1]) == "main"), "")
        if not database_path:
            raise DeletionRejectedError(claim_id, "ledger_path_required")
        return default_tombstone_ledger_path(database_path)

    def _idempotent_replay(self, claim_id: str) -> DeletionResult:
        state = self.connection.execute(
            "SELECT ledger_id,schema_version FROM deletion_ledger_state WHERE singleton=1"
        ).fetchone()
        if state is None:
            raise NotFoundError(f"memory not found: {claim_id}")
        ledger = self._bound_ledger(claim_id)
        try:
            entry = ledger.find_by_claim_id(claim_id)
        except TombstoneLedgerError as error:
            raise DeletionRejectedError(claim_id, "ledger_invalid", str(error)) from error
        if entry is None:
            raise NotFoundError(f"memory not found: {claim_id}")
        return self._replay_result(claim_id, entry)

    @staticmethod
    def _replay_result(claim_id: str, entry: TombstoneEntry) -> DeletionResult:
        return DeletionResult(
            claim_id=claim_id,
            deleted=False,
            already_deleted=True,
            identity_hash=entry.identity_hash,
            deleted_event_ids=entry.event_ids,
        )

    def _delete_closure(self, claim_id: str, event_ids: tuple[str, ...]) -> None:
        stale_observations(self.connection, claim_id, commit=False)
        relation_ids = [
            str(row[0])
            for row in self.connection.execute(
                "SELECT id FROM memory_relations WHERE from_id=? OR to_id=?",
                (claim_id, claim_id),
            ).fetchall()
        ]
        conflict_case_ids = [
            str(row[0])
            for row in self.connection.execute(
                "SELECT id FROM conflict_cases WHERE left_claim_id=? OR right_claim_id=?",
                (claim_id, claim_id),
            ).fetchall()
        ]
        proposal_conditions = [
            "source_claim_id=?",
            "target_claim_id=?",
            "EXISTS (SELECT 1 FROM json_each(supporting_claim_ids_json) WHERE value=?)",
        ]
        proposal_parameters: list[Any] = [claim_id, claim_id, claim_id]
        if relation_ids:
            proposal_conditions.append(f"relation_id IN ({','.join('?' for _ in relation_ids)})")
            proposal_parameters.extend(relation_ids)
        if conflict_case_ids:
            proposal_conditions.append(f"conflict_case_id IN ({','.join('?' for _ in conflict_case_ids)})")
            proposal_parameters.extend(conflict_case_ids)
        self.connection.execute(
            f"DELETE FROM relation_proposals WHERE {' OR '.join(proposal_conditions)}",
            proposal_parameters,
        )
        self.connection.execute(
            "DELETE FROM memory_relations WHERE from_id=? OR to_id=?",
            (claim_id, claim_id),
        )
        self.connection.execute(
            "DELETE FROM conflict_cases WHERE left_claim_id=? OR right_claim_id=?",
            (claim_id, claim_id),
        )
        self.connection.execute(
            "DELETE FROM dedup_pairs WHERE left_claim_id=? OR right_claim_id=?",
            (claim_id, claim_id),
        )
        self.connection.execute(
            "DELETE FROM consolidation_pairs WHERE left_claim_id=? OR right_claim_id=?",
            (claim_id, claim_id),
        )
        self.connection.execute(
            "DELETE FROM evidence_links WHERE "
            "(derived_type='claim' AND derived_id=?) "
            "OR (evidence_type='claim' AND evidence_id=?)",
            (claim_id, claim_id),
        )
        self.connection.execute(
            "DELETE FROM memory_usefulness WHERE memory_type='claim' AND memory_id=?",
            (claim_id,),
        )
        self.connection.execute(
            "DELETE FROM retrieval_feedback WHERE memory_type='claim' AND memory_id=?",
            (claim_id,),
        )
        self.connection.execute("UPDATE claims SET supersedes_id=NULL WHERE supersedes_id=?", (claim_id,))
        self.connection.execute(
            "UPDATE claims SET superseded_by_id=NULL WHERE superseded_by_id=?",
            (claim_id,),
        )
        ClaimRepository(self.connection).delete_vector(claim_id)
        cursor = self.connection.execute("DELETE FROM claims WHERE id=?", (claim_id,))
        if cursor.rowcount != 1:
            raise NotFoundError(f"memory not found: {claim_id}")
        self.connection.execute("DELETE FROM claim_vector_dirty WHERE claim_id=?", (claim_id,))
        if event_ids:
            placeholders = ",".join("?" for _ in event_ids)
            self.connection.execute(f"DELETE FROM events WHERE id IN ({placeholders})", event_ids)
