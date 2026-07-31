"""Read-only integrity checks for Claim search projections and embeddings."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from hl_mem.domain.claims.claim import IndexTextMode, build_index_text

RECALLABLE_CLAIM_STATUSES = ("active", "superseded", "expired")


@dataclass(frozen=True)
class IndexIntegrityReport:
    """Structured result of a projection, embedding, and FTS integrity scan."""

    mode: IndexTextMode
    expected_model: str
    expected_dim: int
    statuses: tuple[str, ...]
    checked: int
    projection_mismatches: int
    projection_errors: int
    missing_embeddings: int
    embedding_length_mismatches: int
    model_dim_mismatches: int
    fts_expected_rows: int
    fts_covered_rows: int | None
    fts_missing_rows: int
    fts_integrity_ok: bool
    fts_error: str | None
    sample_ids: dict[str, tuple[str, ...]]

    @property
    def ok(self) -> bool:
        """Return whether every checked projection and index invariant holds."""
        return (
            self.projection_mismatches == 0
            and self.projection_errors == 0
            and self.missing_embeddings == 0
            and self.embedding_length_mismatches == 0
            and self.model_dim_mismatches == 0
            and self.fts_missing_rows == 0
            and self.fts_integrity_ok
            and self.fts_error is None
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a stable, JSON-serializable report for CLI callers."""
        result = asdict(self)
        result["ok"] = self.ok
        return result


def _decode_claim(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "subject_entity_id": row["subject_entity_id"],
        "predicate": row["predicate"],
        "value": json.loads(row["value_json"]) if row["value_json"] is not None else None,
        "qualifiers": json.loads(row["qualifiers_json"] or "{}"),
        "canonical_slot": row["canonical_slot"],
        "topic_tags": json.loads(row["topic_tags_json"] or "[]"),
    }


def _rows_as_dicts(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    columns = tuple(column[0] for column in cursor.description or ())
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _append_sample(
    samples: dict[str, list[str]],
    issue: str,
    claim_id: str,
    sample_limit: int,
) -> None:
    if len(samples.setdefault(issue, [])) < sample_limit:
        samples[issue].append(claim_id)


def _check_fts_integrity(connection: sqlite3.Connection) -> tuple[bool, str | None]:
    """Run FTS5's external-content integrity check on an in-memory snapshot."""
    snapshot = sqlite3.connect(":memory:")
    try:
        connection.backup(snapshot)
        snapshot.execute("INSERT INTO claims_fts(claims_fts, rank) VALUES ('integrity-check', 1)")
    except sqlite3.Error as error:
        return False, str(error)
    finally:
        snapshot.close()
    return True, None


def check_index_integrity(
    connection: sqlite3.Connection,
    *,
    mode: IndexTextMode,
    expected_model: str,
    expected_dim: int,
    statuses: Sequence[str] = RECALLABLE_CLAIM_STATUSES,
    sample_limit: int = 20,
) -> IndexIntegrityReport:
    """Validate recallable Claim projections without writing to the source DB.

    Projection and embedding checks cover ``active``, ``superseded``, and
    ``expired`` Claims by default. FTS5's strong external-content integrity
    command is executed only against an in-memory SQLite backup.
    """
    if mode not in {"legacy", "value_only", "natural", "answerable"}:
        raise ValueError(f"unsupported index_text mode: {mode}")
    if not expected_model.strip():
        raise ValueError("expected_model must not be empty")
    if expected_dim < 1:
        raise ValueError("expected_dim must be positive")
    if sample_limit < 1:
        raise ValueError("sample_limit must be positive")
    normalized_statuses = tuple(dict.fromkeys(statuses))
    if not normalized_statuses:
        raise ValueError("statuses must not be empty")

    placeholders = ",".join("?" for _ in normalized_statuses)
    rows = _rows_as_dicts(
        connection.execute(
            "SELECT id,subject_entity_id,predicate,value_json,qualifiers_json,"
            "canonical_slot,topic_tags_json,index_text,embedding_dense,"
            "embedding_model,embedding_dim "
            f"FROM claims WHERE status IN ({placeholders}) ORDER BY id",
            normalized_statuses,
        )
    )

    projection_mismatches = 0
    projection_errors = 0
    missing_embeddings = 0
    embedding_length_mismatches = 0
    model_dim_mismatches = 0
    samples: dict[str, list[str]] = {}
    for row in rows:
        claim_id = str(row["id"])
        try:
            projected = build_index_text(_decode_claim(row), mode=mode)
        except (json.JSONDecodeError, TypeError, ValueError):
            projection_errors += 1
            _append_sample(samples, "projection_errors", claim_id, sample_limit)
        else:
            if row["index_text"] != projected:
                projection_mismatches += 1
                _append_sample(
                    samples,
                    "projection_mismatches",
                    claim_id,
                    sample_limit,
                )

        embedding = row["embedding_dense"]
        if embedding is None:
            missing_embeddings += 1
            _append_sample(samples, "missing_embeddings", claim_id, sample_limit)
        else:
            stored_dim = row["embedding_dim"]
            if (
                not isinstance(embedding, (bytes, bytearray, memoryview))
                or not isinstance(stored_dim, int)
                or stored_dim < 1
                or len(embedding) != stored_dim * 4
            ):
                embedding_length_mismatches += 1
                _append_sample(
                    samples,
                    "embedding_length_mismatches",
                    claim_id,
                    sample_limit,
                )

        if row["embedding_model"] != expected_model or row["embedding_dim"] != expected_dim:
            model_dim_mismatches += 1
            _append_sample(
                samples,
                "model_dim_mismatches",
                claim_id,
                sample_limit,
            )

    fts_expected_rows = len(rows)
    fts_covered_rows: int | None = None
    fts_missing_rows = 0
    fts_error: str | None = None
    try:
        coverage = connection.execute(
            "SELECT count(*) AS expected_rows,"
            "count(claims_fts_docsize.id) AS covered_rows "
            "FROM claims LEFT JOIN claims_fts_docsize "
            "ON claims_fts_docsize.id=claims.rowid "
            f"WHERE claims.status IN ({placeholders})",
            normalized_statuses,
        ).fetchone()
        assert coverage is not None
        fts_expected_rows = int(coverage[0])
        fts_covered_rows = int(coverage[1])
        fts_missing_rows = fts_expected_rows - fts_covered_rows
        missing_fts_rows = connection.execute(
            "SELECT claims.id FROM claims LEFT JOIN claims_fts_docsize "
            "ON claims_fts_docsize.id=claims.rowid "
            f"WHERE claims.status IN ({placeholders}) "
            "AND claims_fts_docsize.id IS NULL ORDER BY claims.id LIMIT ?",
            (*normalized_statuses, sample_limit),
        ).fetchall()
        for missing_row in missing_fts_rows:
            _append_sample(
                samples,
                "fts_missing_rows",
                str(missing_row[0]),
                sample_limit,
            )
    except sqlite3.Error as error:
        fts_error = str(error)

    fts_integrity_ok, integrity_error = _check_fts_integrity(connection)
    if integrity_error is not None:
        fts_error = integrity_error if fts_error is None else f"{fts_error}; integrity-check: {integrity_error}"

    return IndexIntegrityReport(
        mode=mode,
        expected_model=expected_model,
        expected_dim=expected_dim,
        statuses=normalized_statuses,
        checked=len(rows),
        projection_mismatches=projection_mismatches,
        projection_errors=projection_errors,
        missing_embeddings=missing_embeddings,
        embedding_length_mismatches=embedding_length_mismatches,
        model_dim_mismatches=model_dim_mismatches,
        fts_expected_rows=fts_expected_rows,
        fts_covered_rows=fts_covered_rows,
        fts_missing_rows=fts_missing_rows,
        fts_integrity_ok=fts_integrity_ok,
        fts_error=fts_error,
        sample_ids={key: tuple(value) for key, value in sorted(samples.items())},
    )
