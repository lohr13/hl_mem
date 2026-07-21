from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from hl_mem.ingest.embeddings import cosine_similarity


def _sanitize_fts_query(query: str) -> str:
    """Quote user-provided tokens so FTS5 treats them as literals."""
    tokens = query.strip().split()
    if not tokens:
        return '""'
    return " ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def _insert(connection: sqlite3.Connection, table: str, data: dict[str, Any]) -> bool:
    columns = ", ".join(data)
    placeholders = ", ".join("?" for _ in data)
    before = connection.total_changes
    connection.execute(
        f"INSERT OR IGNORE INTO {table} ({columns}) VALUES ({placeholders})",
        tuple(data.values()),
    )
    connection.commit()
    return connection.total_changes > before
class EventRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def insert_event(self, event: dict[str, Any]) -> bool:
        return _insert(self.connection, "events", event)

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        return _row(self.connection.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone())

    def get_recent_events(
        self, session_id: str, before: dict[str, Any], limit: int
    ) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM events WHERE session_id=? AND "
            "(occurred_at<? OR (occurred_at=? AND id<?)) "
            "ORDER BY occurred_at DESC,id DESC LIMIT ?",
            (session_id, before["occurred_at"], before["occurred_at"], before["id"], limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def search_events_fts(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        try:
            rows = self.connection.execute(
                "SELECT e.* FROM events_fts f JOIN events e ON e.rowid=f.rowid "
                "WHERE events_fts MATCH ? ORDER BY bm25(events_fts) LIMIT ?",
                (_sanitize_fts_query(query), limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(row) for row in rows]
class ClaimRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def insert_claim(self, claim: dict[str, Any]) -> bool:
        return _insert(self.connection, "claims", claim)

    def get_claim(self, claim_id: str) -> dict[str, Any] | None:
        return _row(self.connection.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone())

    def update_status(self, claim_id: str, status: str) -> bool:
        cursor = self.connection.execute("UPDATE claims SET status=? WHERE id=?", (status, claim_id))
        self.connection.commit()
        return cursor.rowcount == 1

    def find_active(self, namespace: str, subject_entity_id: str | None) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM claims WHERE namespace_key=? AND subject_entity_id IS ? "
            "AND status='active'", (namespace, subject_entity_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def find_by_conflict_key(self, conflict_key: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM claims WHERE conflict_key=? AND status IN ('active','candidate','disputed')",
            (conflict_key,),
        ).fetchall()
        return [dict(row) for row in rows]

    def find_by_fact_hash(self, namespace: str, fact_hash: str) -> dict[str, Any] | None:
        return _row(self.connection.execute(
            "SELECT * FROM claims WHERE namespace_key=? AND fact_hash=? "
            "AND status IN ('active','candidate','disputed') ORDER BY recorded_from DESC LIMIT 1",
            (namespace, fact_hash),
        ).fetchone())

    def list_embedded(self, as_of: str | None = None) -> list[dict[str, Any]]:
        reference = as_of or datetime.now(timezone.utc).isoformat()
        statuses = "('active','disputed','superseded')" if as_of else "('active','disputed')"
        rows = self.connection.execute(
            f"SELECT * FROM claims WHERE embedding_dense IS NOT NULL AND status IN {statuses} "
            "AND (valid_from IS NULL OR valid_from<=?) AND (valid_to IS NULL OR valid_to>?) "
            "AND (expires_at IS NULL OR expires_at>?)", (reference, reference, reference),
        ).fetchall()
        return [dict(row) for row in rows]

    def search_claims_vector(
        self, query_blob: bytes, limit: int = 200, as_of: str | None = None
    ) -> list[dict[str, Any]]:
        # A 100k x 2048 float32 full scan is about 819 MB; indexed retrieval must
        # be reconsidered before deployments approach that scale.
        return sorted(self.list_embedded(as_of),
                      key=lambda claim: cosine_similarity(query_blob, claim["embedding_dense"]),
                      reverse=True)[:limit]

    def record_access(self, claim_ids: list[str], accessed_at: str) -> int:
        unique_ids = list(dict.fromkeys(claim_ids))
        total = 0
        try:
            for start in range(0, len(unique_ids), 500):
                chunk = unique_ids[start:start + 500]
                if not chunk:
                    continue
                placeholders = ",".join("?" for _ in chunk)
                cursor = self.connection.execute(
                    "UPDATE claims SET access_count=access_count+1,last_accessed_at=? "
                    f"WHERE id IN ({placeholders}) "
                    "AND status IN ('active','disputed','superseded')",
                    (accessed_at, *chunk),
                )
                total += cursor.rowcount
            self.connection.commit()
            return total
        except Exception:
            self.connection.rollback()
            raise

    def supersede(self, old_id: str, new_valid_from: str) -> None:
        self.connection.execute(
            "UPDATE claims SET status='superseded',valid_to=?,recorded_to=? WHERE id=?",
            (new_valid_from, new_valid_from, old_id),
        )
        self.connection.commit()

    def retract(self, claim_id: str) -> bool:
        cursor = self.connection.execute(
            "UPDATE claims SET status='retracted',embedding_dense=NULL,embedding_sparse=NULL WHERE id=?",
            (claim_id,),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def search_claims_fts(
        self, query: str, limit: int = 20, as_of: str | None = None
    ) -> list[dict[str, Any]]:
        reference = as_of or datetime.now(timezone.utc).isoformat()
        statuses = "('active','disputed','superseded')" if as_of else "('active','disputed')"
        try:
            rows = self.connection.execute(
                "SELECT c.* FROM claims_fts f JOIN claims c ON c.rowid=f.rowid "
                f"WHERE claims_fts MATCH ? AND c.status IN {statuses} "
                "AND (c.valid_from IS NULL OR c.valid_from<=?) "
                "AND (c.valid_to IS NULL OR c.valid_to>?) "
                "AND (c.expires_at IS NULL OR c.expires_at>?) "
                "ORDER BY bm25(claims_fts) LIMIT ?",
                (_sanitize_fts_query(query), reference, reference, reference, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(row) for row in rows]
class EvidenceRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add_link(self, link: dict[str, Any]) -> bool:
        return _insert(self.connection, "evidence_links", link)

    def get_links_for_derived(self, derived_type: str, derived_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM evidence_links WHERE derived_type=? AND derived_id=?",
            (derived_type, derived_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_links_for_evidence(self, evidence_type: str, evidence_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM evidence_links WHERE evidence_type=? AND evidence_id=?",
            (evidence_type, evidence_id),
        ).fetchall()
        return [dict(row) for row in rows]
class JobRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def insert_job(self, job: dict[str, Any]) -> bool:
        return _insert(self.connection, "jobs", job)

    def lease_job(self, leased_until: str, updated_at: str) -> dict[str, Any] | None:
        """Atomically claim the oldest runnable job across worker processes."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT id FROM jobs WHERE (status='pending' OR "
                "(status='running' AND leased_until<?)) "
                "AND (run_after IS NULL OR run_after<=?) ORDER BY created_at,id LIMIT 1",
                (updated_at, updated_at),
            ).fetchone()
            if not row:
                self.connection.commit()
                return None
            cursor = self.connection.execute(
                "UPDATE jobs SET status='running',leased_until=?,updated_at=?,attempts=attempts+1 "
                "WHERE id=? AND (status='pending' OR (status='running' AND leased_until<?))",
                (leased_until, updated_at, row["id"], updated_at),
            )
            self.connection.commit()
            if cursor.rowcount != 1:
                return None
            return _row(self.connection.execute(
                "SELECT * FROM jobs WHERE id=?", (row["id"],)
            ).fetchone())
        except Exception:
            self.connection.rollback()
            raise

    def complete_job(self, job_id: str, updated_at: str) -> bool:
        return self._finish(job_id, "succeeded", updated_at, None)

    def fail_job(self, job_id: str, error: str, updated_at: str) -> bool:
        row = self.connection.execute("SELECT attempts,max_attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
        status = "dead" if row and row["attempts"] >= row["max_attempts"] else "pending"
        return self._finish(job_id, status, updated_at, error)

    def counts(self) -> dict[str, int]:
        counts = {key: 0 for key in ("pending", "running", "failed", "dead")}
        rows = self.connection.execute(
            "SELECT status,count(*) AS count FROM jobs GROUP BY status").fetchall()
        for row in rows:
            if row["status"] in counts:
                counts[row["status"]] = row["count"]
        return counts

    def _finish(self, job_id: str, status: str, updated_at: str, error: str | None) -> bool:
        cursor = self.connection.execute(
            "UPDATE jobs SET status=?,updated_at=?,last_error=?,leased_until=NULL WHERE id=?",
            (status, updated_at, error, job_id),
        )
        self.connection.commit()
        return cursor.rowcount == 1
class DerivationRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def insert_observation(self, observation: dict[str, Any]) -> bool:
        return _insert(self.connection, "derivations", {"kind": "observation", **observation})

    def get_observation(self, observation_id: str) -> dict[str, Any] | None:
        return _row(self.connection.execute("SELECT * FROM derivations WHERE id=?", (observation_id,)).fetchone())

    def update_status(self, observation_id: str, status: str) -> bool:
        cursor = self.connection.execute("UPDATE derivations SET status=? WHERE id=?", (status, observation_id))
        self.connection.commit()
        return cursor.rowcount == 1
