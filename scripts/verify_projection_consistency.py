"""Verify that every recallable Claim uses one configured search projection.

The verifier is deliberately read-only.  It opens the requested SQLite
database with ``mode=ro`` and runs FTS5's write-shaped integrity command only
against the in-memory snapshot created by ``check_index_integrity``.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hl_mem.domain.claims.claim import IndexTextMode
from hl_mem.workers.index_integrity import (
    RECALLABLE_CLAIM_STATUSES,
    check_index_integrity,
)

EXPECTATION_SOURCE = "explicit_arguments"
PROJECTION_CONSISTENCY_DEFINITION = (
    "all recallable stored index_text values exactly equal the selected current projector output"
)


class _RaisingArgumentParser(argparse.ArgumentParser):
    """Keep argument failures on the JSON reporting path."""

    def error(self, message: str) -> None:
        raise ValueError(message)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse explicit verification inputs and safe, non-secret defaults."""
    parser = _RaisingArgumentParser(
        description="Verify Claim projection, embedding, and FTS consistency.",
    )
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--mode", required=True)
    parser.add_argument(
        "--projection-version",
        "--index-text-version",
        "--version",
        dest="projection_version",
        required=True,
    )
    parser.add_argument(
        "--embedding-model",
        "--model",
        dest="embedding_model",
        required=True,
    )
    parser.add_argument(
        "--embedding-dim",
        "--dim",
        dest="embedding_dim",
        type=_positive_int,
        required=True,
    )
    parser.add_argument("--sample-limit", type=_positive_int, default=20)
    parser.add_argument("--report-path", type=Path)
    arguments = parser.parse_args(argv)
    if arguments.mode not in {"legacy", "value_only", "natural", "answerable"}:
        raise ValueError(f"unsupported index_text mode: {arguments.mode}")
    if not str(arguments.projection_version).strip():
        raise ValueError("projection version must not be empty")
    if not str(arguments.embedding_model).strip():
        raise ValueError("embedding model must not be empty")
    return arguments


def open_readonly_database(database_path: Path) -> sqlite3.Connection:
    """Open an existing SQLite database without migrations or source writes."""
    resolved = database_path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _embedding_distributions(
    connection: sqlite3.Connection,
    statuses: Sequence[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    placeholders = ",".join("?" for _ in statuses)
    model_dim_rows = connection.execute(
        "SELECT embedding_model,embedding_dim,count(*) AS claim_count "
        f"FROM claims WHERE status IN ({placeholders}) "
        "GROUP BY embedding_model,embedding_dim "
        "ORDER BY embedding_model,embedding_dim",
        tuple(statuses),
    ).fetchall()
    blob_length_rows = connection.execute(
        "SELECT length(embedding_dense) AS blob_bytes,count(*) AS claim_count "
        f"FROM claims WHERE status IN ({placeholders}) "
        "GROUP BY length(embedding_dense) ORDER BY blob_bytes",
        tuple(statuses),
    ).fetchall()
    return (
        [
            {
                "model": row["embedding_model"],
                "dim": row["embedding_dim"],
                "claim_count": int(row["claim_count"]),
            }
            for row in model_dim_rows
        ],
        [
            {
                "blob_bytes": row["blob_bytes"],
                "claim_count": int(row["claim_count"]),
            }
            for row in blob_length_rows
        ],
    )


def verify_projection_consistency(
    database_path: Path,
    *,
    mode: IndexTextMode,
    projection_version: str,
    expected_model: str,
    expected_dim: int,
    sample_limit: int = 20,
) -> dict[str, Any]:
    """Return a machine-readable, read-only projection consistency report."""
    statuses = tuple(RECALLABLE_CLAIM_STATUSES)
    connection = open_readonly_database(database_path)
    try:
        model_dim_distribution, blob_length_distribution = _embedding_distributions(
            connection,
            statuses,
        )
        integrity = check_index_integrity(
            connection,
            mode=mode,
            expected_model=expected_model,
            expected_dim=expected_dim,
            statuses=statuses,
            sample_limit=sample_limit,
        )
    finally:
        connection.close()

    single_model_dim = len(model_dim_distribution) == 1
    model_dim_matches_expected = bool(
        single_model_dim
        and integrity.checked > 0
        and model_dim_distribution[0]["model"] == expected_model
        and model_dim_distribution[0]["dim"] == expected_dim
        and model_dim_distribution[0]["claim_count"] == integrity.checked
    )
    empty_recallable_dataset = integrity.checked == 0
    projection_inconsistency_detected = bool(integrity.projection_mismatches or integrity.projection_errors)
    uniform_current_projection = not projection_inconsistency_detected
    # The schema has no per-Claim version column.  Exact agreement with the
    # selected current projector proves a uniform current projection; once a
    # mismatch exists, content alone cannot distinguish mixed from uniformly
    # stale provenance.
    mixed_projection_detected: bool | None = False if uniform_current_projection else None
    ok = bool(
        integrity.ok and model_dim_matches_expected and uniform_current_projection and not empty_recallable_dataset
    )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ok": ok,
        "database": str(database_path.resolve()),
        "recallable_statuses": list(statuses),
        "expected_projection_version": projection_version,
        "expectation_source": EXPECTATION_SOURCE,
        "projection_version_source": EXPECTATION_SOURCE,
        "projection_version_persisted_per_claim": False,
        "deployment_configuration_verified": False,
        "empty_recallable_dataset": empty_recallable_dataset,
        "projection": {
            "mode": mode,
            "expected_version": projection_version,
            "expectation_source": EXPECTATION_SOURCE,
            "version_persisted_per_claim": False,
            "consistency_definition": PROJECTION_CONSISTENCY_DEFINITION,
            "uniform_current_projection": uniform_current_projection,
            "inconsistency_detected": projection_inconsistency_detected,
            "mixed_projection_detected": mixed_projection_detected,
            "mixed_detection_note": (
                "no per-Claim version is stored; mismatches fail verification "
                "but cannot be classified as mixed versus uniformly stale"
            ),
            "checked": integrity.checked,
            "mismatches": integrity.projection_mismatches,
            "errors": integrity.projection_errors,
        },
        "mixed_projection_detected": mixed_projection_detected,
        "projection_inconsistency_detected": projection_inconsistency_detected,
        "embedding": {
            "expected_model": expected_model,
            "expected_dim": expected_dim,
            "model_dim_distribution": model_dim_distribution,
            "blob_length_distribution": blob_length_distribution,
            "single_model_dim": single_model_dim,
            "model_dim_matches_expected": model_dim_matches_expected,
        },
        "integrity": integrity.to_dict(),
    }


def _error_report(
    error: Exception,
    arguments: argparse.Namespace | None,
) -> dict[str, Any]:
    mode = getattr(arguments, "mode", None)
    projection_version = getattr(arguments, "projection_version", None)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ok": False,
        "database": (
            str(arguments.database.resolve())
            if arguments is not None and getattr(arguments, "database", None)
            else None
        ),
        "recallable_statuses": list(RECALLABLE_CLAIM_STATUSES),
        "expected_projection_version": projection_version,
        "expectation_source": EXPECTATION_SOURCE,
        "projection_version_source": EXPECTATION_SOURCE,
        "projection_version_persisted_per_claim": False,
        "deployment_configuration_verified": False,
        "projection": {
            "mode": mode,
            "expected_version": projection_version,
            "expectation_source": EXPECTATION_SOURCE,
            "version_persisted_per_claim": False,
            "consistency_definition": PROJECTION_CONSISTENCY_DEFINITION,
            "uniform_current_projection": None,
            "inconsistency_detected": None,
            "mixed_projection_detected": None,
        },
        "mixed_projection_detected": None,
        "error": {
            "type": type(error).__name__,
            "message": str(error),
        },
    }


def _write_report(
    report: dict[str, Any],
    report_path: Path,
    database_path: Path,
) -> None:
    resolved_report = report_path.resolve()
    resolved_database = database_path.resolve()
    protected = (
        resolved_database,
        Path(f"{resolved_database}-wal"),
        Path(f"{resolved_database}-shm"),
        Path(f"{resolved_database}-journal"),
    )
    if any(_paths_alias(resolved_report, path) for path in protected):
        raise ValueError("report path must not overwrite the database or its sidecars")
    resolved_report.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=resolved_report.parent,
            prefix=f".{resolved_report.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, resolved_report)
    finally:
        if temporary_path is not None and temporary_path.exists():
            try:
                temporary_path.unlink()
            except OSError:
                pass


def _paths_alias(left: Path, right: Path) -> bool:
    if left == right:
        return True
    if left.exists() and right.exists():
        try:
            return left.samefile(right)
        except OSError:
            return False
    return False


def main(argv: Sequence[str] | None = None) -> int:
    """Run the verifier, always emitting exactly one JSON document to stdout."""
    arguments: argparse.Namespace | None = None
    try:
        arguments = parse_args(argv)
        report = verify_projection_consistency(
            arguments.database,
            mode=arguments.mode,
            projection_version=arguments.projection_version,
            expected_model=arguments.embedding_model,
            expected_dim=arguments.embedding_dim,
            sample_limit=arguments.sample_limit,
        )
    except Exception as error:
        report = _error_report(error, arguments)

    if arguments is not None and arguments.report_path is not None:
        try:
            _write_report(report, arguments.report_path, arguments.database)
        except Exception as error:
            report["ok"] = False
            report["report_error"] = {
                "type": type(error).__name__,
                "message": str(error),
            }

    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
