"""Repair the two approved v0.19 double-encoded Claim values.

The script is intentionally narrow and defaults to a read-only dry-run.  Apply
mode uses one ``BEGIN IMMEDIATE`` transaction and aborts the whole batch when
either Claim no longer matches the frozen before-value hash.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hl_mem.storage.migrations.fact_hash_v2 import compute_fact_hash_v2

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST_PATH = PROJECT_ROOT / "tests" / "eval" / "repair_manifest_v019.json"
APPROVED_VALUE_HASHES = {
    "697dc55a33c84a78a536ca5eb2296ad9": {
        "before": "3e66937d090d62b53415402f4343ec1903f569874b69c34f2eca7469a0108475",
        "target": "82c6ef0ca423395fa43fa42deb7f8f7cf7509cadbac6fffeffc01fee7def5495",
    },
    "e78d567879c740a28d342d6b872ba9a0": {
        "before": "30cf91fa29cc757473b67ee2d58f59e4bf74b64de03b7ab3d3f124d8f808be37",
        "target": "31650807909e8636631a6c5876a2098564e261fac605cd87845c4bcd11252f53",
    },
}
APPROVED_CLAIM_IDS = frozenset(APPROVED_VALUE_HASHES)
MUTABLE_CLAIM_COLUMNS = frozenset({"value_json", "fact_hash"})


@dataclass(frozen=True)
class RepairEntry:
    """One frozen manifest repair."""

    claim_id: str
    issue: str
    current_value_json: str
    target_value_json: str

    @property
    def before_hash(self) -> str:
        return _sha256_text(self.current_value_json)

    @property
    def target_hash(self) -> str:
        return _sha256_text(self.target_value_json)

    @property
    def target_value(self) -> str:
        value = json.loads(self.target_value_json)
        assert isinstance(value, str)
        return value


@dataclass(frozen=True)
class RepairManifest:
    """Validated frozen manifest and its source-file digest."""

    path: Path
    sha256: str
    description: str
    repairs: tuple[RepairEntry, ...]


class _RaisingArgumentParser(argparse.ArgumentParser):
    """Keep argument errors on the JSON reporting path."""

    def error(self, message: str) -> None:
        raise ValueError(message)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_sql_value(value: Any) -> Any:
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, (bytes, bytearray)):
        blob = bytes(value)
        return {
            "blob_length": len(blob),
            "blob_sha256": hashlib.sha256(blob).hexdigest(),
        }
    return value


def _query_digest(
    connection: sqlite3.Connection,
    query: str,
    parameters: Sequence[Any] = (),
    *,
    excluded_columns: frozenset[str] = frozenset(),
) -> tuple[str, int]:
    digest = hashlib.sha256()
    cursor = connection.execute(query, tuple(parameters))
    columns = tuple(column[0] for column in cursor.description or ())
    count = 0
    for row in cursor:
        payload = {
            column: _stable_sql_value(row[index])
            for index, column in enumerate(columns)
            if column not in excluded_columns
        }
        digest.update(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")
        count += 1
    return digest.hexdigest(), count


def _read_json_string(value: Any, field: str, claim_id: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"manifest repair {claim_id}: {field} must be a string")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError(f"manifest repair {claim_id}: {field} is invalid JSON") from error
    if not isinstance(decoded, str):
        raise ValueError(f"manifest repair {claim_id}: {field} must encode a JSON string")
    return value


def load_manifest(manifest_path: Path = DEFAULT_MANIFEST_PATH) -> RepairManifest:
    """Load and strictly validate the frozen two-row repair manifest."""
    resolved = manifest_path.resolve()
    raw = resolved.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid UTF-8 JSON repair manifest: {resolved}") from error
    if not isinstance(payload, dict):
        raise ValueError("repair manifest root must be an object")
    raw_repairs = payload.get("repairs")
    if not isinstance(raw_repairs, list):
        raise ValueError("repair manifest repairs must be a list")

    repairs: list[RepairEntry] = []
    seen_ids: set[str] = set()
    for item in raw_repairs:
        if not isinstance(item, dict):
            raise ValueError("each manifest repair must be an object")
        claim_id = item.get("claim_id")
        if not isinstance(claim_id, str) or not claim_id:
            raise ValueError("each manifest repair must have a non-empty claim_id")
        if claim_id in seen_ids:
            raise ValueError(f"duplicate manifest claim_id: {claim_id}")
        seen_ids.add(claim_id)
        if claim_id not in APPROVED_CLAIM_IDS:
            raise ValueError(f"unapproved manifest claim_id: {claim_id}")
        issue = item.get("issue")
        if not isinstance(issue, str) or not issue:
            raise ValueError(f"manifest repair {claim_id}: issue must be a non-empty string")
        current_value_json = _read_json_string(
            item.get("current_value_json"),
            "current_value_json",
            claim_id,
        )
        target_value_json = _read_json_string(
            item.get("target_value_json"),
            "target_value_json",
            claim_id,
        )
        entry = RepairEntry(
            claim_id=claim_id,
            issue=issue,
            current_value_json=current_value_json,
            target_value_json=target_value_json,
        )
        approved_hashes = APPROVED_VALUE_HASHES[claim_id]
        if entry.before_hash != approved_hashes["before"]:
            raise ValueError(f"manifest repair {claim_id}: frozen before-value hash changed")
        if entry.target_hash != approved_hashes["target"]:
            raise ValueError(f"manifest repair {claim_id}: frozen target-value hash changed")
        repairs.append(entry)

    if seen_ids != APPROVED_CLAIM_IDS or len(repairs) != len(APPROVED_CLAIM_IDS):
        missing = sorted(APPROVED_CLAIM_IDS - seen_ids)
        raise ValueError(f"repair manifest must contain exactly the two approved IDs; missing={missing}")
    repairs.sort(key=lambda entry: entry.claim_id)
    return RepairManifest(
        path=resolved,
        sha256=hashlib.sha256(raw).hexdigest(),
        description=str(payload.get("description") or ""),
        repairs=tuple(repairs),
    )


def _open_database(database_path: Path, *, apply: bool) -> sqlite3.Connection:
    resolved = database_path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    mode = "rw" if apply else "ro"
    connection = sqlite3.connect(
        f"{resolved.as_uri()}?mode={mode}",
        uri=True,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    if not apply:
        connection.execute("PRAGMA query_only=ON")
    return connection


def _batch_digests(
    connection: sqlite3.Connection,
    claim_ids: Sequence[str],
) -> dict[str, Any]:
    placeholders = ",".join("?" for _ in claim_ids)
    protected_claims, protected_count = _query_digest(
        connection,
        f"SELECT * FROM claims WHERE id IN ({placeholders}) ORDER BY id",
        claim_ids,
        excluded_columns=MUTABLE_CLAIM_COLUMNS,
    )
    non_target_values, non_target_count = _query_digest(
        connection,
        f"SELECT id,value_json FROM claims WHERE id NOT IN ({placeholders}) ORDER BY id",
        claim_ids,
    )
    evidence, evidence_count = _query_digest(
        connection,
        "SELECT * FROM evidence_links ORDER BY id",
    )
    canonical_claims, canonical_count = _query_digest(
        connection,
        "SELECT * FROM claims ORDER BY id",
        excluded_columns=frozenset(
            {
                "index_text",
                "embedding_dense",
                "embedding_sparse",
                "embedding_model",
                "embedding_dim",
            }
        ),
    )
    return {
        "protected_target_claims": {
            "sha256": protected_claims,
            "row_count": protected_count,
        },
        "non_target_values": {
            "sha256": non_target_values,
            "row_count": non_target_count,
        },
        "evidence": {
            "sha256": evidence,
            "row_count": evidence_count,
        },
        "canonical_claims": {
            "sha256": canonical_claims,
            "row_count": canonical_count,
        },
    }


def _row_snapshot(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    protected = {key: _stable_sql_value(row[key]) for key in row.keys() if key not in MUTABLE_CLAIM_COLUMNS}
    protected_digest = hashlib.sha256(
        json.dumps(
            protected,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "value_json": row["value_json"],
        "value_json_sha256": (_sha256_text(row["value_json"]) if isinstance(row["value_json"], str) else None),
        "fact_hash": row["fact_hash"],
        "conflict_key": row["conflict_key"],
        "conflict_key_version": row["conflict_key_version"],
        "legacy_conflict_key": row["legacy_conflict_key"],
        "status": row["status"],
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
        "recorded_from": row["recorded_from"],
        "recorded_to": row["recorded_to"],
        "scope": row["scope"],
        "importance": row["importance"],
        "expires_at": row["expires_at"],
        "protected_fields_sha256": protected_digest,
    }


def _select_target_rows(
    connection: sqlite3.Connection,
    claim_ids: Sequence[str],
) -> dict[str, sqlite3.Row]:
    placeholders = ",".join("?" for _ in claim_ids)
    rows = connection.execute(
        f"SELECT * FROM claims WHERE id IN ({placeholders}) ORDER BY id",
        tuple(claim_ids),
    ).fetchall()
    return {str(row["id"]): row for row in rows}


def _target_fact_hash(row: sqlite3.Row, entry: RepairEntry) -> str:
    return compute_fact_hash_v2(
        str(row["subject_entity_id"] or ""),
        str(row["predicate"] or ""),
        entry.target_value,
    )


def _current_fact_hash(row: sqlite3.Row) -> str | None:
    raw_value = row["value_json"]
    if not isinstance(raw_value, str):
        return None
    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError:
        return None
    return compute_fact_hash_v2(
        str(row["subject_entity_id"] or ""),
        str(row["predicate"] or ""),
        value,
    )


def _collision_ids(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    target_fact_hash: str,
) -> list[str]:
    rows = connection.execute(
        "SELECT id FROM claims WHERE namespace_key IS ? AND fact_hash=? AND id<>? ORDER BY id",
        (row["namespace_key"], target_fact_hash, row["id"]),
    ).fetchall()
    return [str(item["id"]) for item in rows]


def _build_row_audits(
    connection: sqlite3.Connection,
    manifest: RepairManifest,
    rows: dict[str, sqlite3.Row],
) -> tuple[list[dict[str, Any]], list[str]]:
    audits: list[dict[str, Any]] = []
    failures: list[str] = []
    target_hashes: dict[tuple[Any, str], list[str]] = {}
    for entry in manifest.repairs:
        row = rows.get(entry.claim_id)
        before = _row_snapshot(row)
        actual_hash = before["value_json_sha256"] if before is not None else None
        hash_matches = actual_hash == entry.before_hash
        target_fact_hash = _target_fact_hash(row, entry) if row is not None else None
        collisions = (
            _collision_ids(connection, row, target_fact_hash)
            if row is not None and target_fact_hash is not None
            else []
        )
        if row is not None and target_fact_hash is not None:
            target_hashes.setdefault((row["namespace_key"], target_fact_hash), []).append(entry.claim_id)
        if row is None:
            failures.append(f"{entry.claim_id}: claim not found")
        elif not hash_matches or row["value_json"] != entry.current_value_json:
            failures.append(f"{entry.claim_id}: before-value hash mismatch")
        if collisions:
            failures.append(f"{entry.claim_id}: target fact_hash collision with {','.join(collisions)}")
        audits.append(
            {
                "claim_id": entry.claim_id,
                "issue": entry.issue,
                "before": before,
                "expected_before": {
                    "value_json": entry.current_value_json,
                    "value_json_sha256": entry.before_hash,
                },
                "hash_check": {
                    "expected": entry.before_hash,
                    "actual": actual_hash,
                    "matches": hash_matches,
                },
                "stored_fact_hash_matches_current_rule": bool(
                    row is not None and row["fact_hash"] == _current_fact_hash(row)
                ),
                "target": {
                    "value_json": entry.target_value_json,
                    "value_json_sha256": entry.target_hash,
                    "fact_hash": target_fact_hash,
                },
                "fact_hash_collision_ids": collisions,
                "conflict_key_recomputed": False,
                "conflict_key_recompute_reason": "value_json is not a conflict-key input",
                "cas_applied": False,
                "after": None,
            }
        )

    for claim_ids in target_hashes.values():
        if len(claim_ids) < 2:
            continue
        for audit in audits:
            if audit["claim_id"] in claim_ids:
                peers = sorted(claim_id for claim_id in claim_ids if claim_id != audit["claim_id"])
                audit["fact_hash_collision_ids"] = sorted(set(audit["fact_hash_collision_ids"]) | set(peers))
        failures.append(f"approved targets collide with each other: {','.join(sorted(claim_ids))}")
    return audits, failures


def _digests_unchanged(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return all(before[key] == after[key] for key in ("protected_target_claims", "non_target_values", "evidence"))


def repair_database(
    database_path: Path,
    *,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    apply: bool = False,
) -> dict[str, Any]:
    """Inspect or atomically apply the frozen two-row repair."""
    manifest = load_manifest(manifest_path)
    claim_ids = tuple(entry.claim_id for entry in manifest.repairs)
    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "operation": "repair_v019_double_encoded_values",
        "audit_stage": "final",
        "mode": "apply" if apply else "dry-run",
        "database": str(database_path.resolve()),
        "manifest": {
            "path": str(manifest.path),
            "sha256": manifest.sha256,
            "description": manifest.description,
            "repair_count": len(manifest.repairs),
        },
        "approved_claim_ids": sorted(APPROVED_CLAIM_IDS),
        "checked": len(manifest.repairs),
        "rows_found": 0,
        "would_apply": 0,
        "applied": 0,
        "ok": False,
        "rolled_back": False,
        "failure": None,
        "repairs": [],
        "batch_before": None,
        "batch_after": None,
        "invariants_unchanged": None,
        "outside_target_value_writes": None,
    }
    connection = _open_database(database_path, apply=apply)
    transaction_started = False
    try:
        connection.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        transaction_started = True
        before_digests = _batch_digests(connection, claim_ids)
        rows = _select_target_rows(connection, claim_ids)
        audits, failures = _build_row_audits(connection, manifest, rows)
        report["rows_found"] = len(rows)
        report["repairs"] = audits
        report["batch_before"] = before_digests
        report["would_apply"] = len(audits) if not failures else 0

        if failures:
            raise RuntimeError("; ".join(failures))
        if not apply:
            connection.rollback()
            transaction_started = False
            report["batch_after"] = before_digests
            report["invariants_unchanged"] = True
            report["outside_target_value_writes"] = 0
            report["ok"] = True
            return report

        connection.create_function(
            "repair_sha256",
            1,
            lambda value: _sha256_text(value) if isinstance(value, str) else None,
            deterministic=True,
        )
        audit_by_id = {audit["claim_id"]: audit for audit in audits}
        for entry in manifest.repairs:
            target_fact_hash = audit_by_id[entry.claim_id]["target"]["fact_hash"]
            cursor = connection.execute(
                "UPDATE claims SET value_json=?,fact_hash=? "
                "WHERE id=? AND repair_sha256(value_json)=? AND value_json=?",
                (
                    entry.target_value_json,
                    target_fact_hash,
                    entry.claim_id,
                    entry.before_hash,
                    entry.current_value_json,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"{entry.claim_id}: compare-and-set failed")
            audit_by_id[entry.claim_id]["cas_applied"] = True

        after_rows = _select_target_rows(connection, claim_ids)
        after_digests = _batch_digests(connection, claim_ids)
        for entry in manifest.repairs:
            audit = audit_by_id[entry.claim_id]
            after = _row_snapshot(after_rows.get(entry.claim_id))
            audit["after"] = after
            if (
                after is None
                or after["value_json"] != entry.target_value_json
                or after["value_json_sha256"] != entry.target_hash
                or after["fact_hash"] != audit["target"]["fact_hash"]
            ):
                raise RuntimeError(f"{entry.claim_id}: post-update verification failed")
            if audit["before"]["protected_fields_sha256"] != after["protected_fields_sha256"]:
                raise RuntimeError(f"{entry.claim_id}: protected Claim fields changed")
        if not _digests_unchanged(before_digests, after_digests):
            raise RuntimeError("protected Claim, non-target value, or evidence digest changed")

        connection.commit()
        transaction_started = False
        report["batch_after"] = after_digests
        report["invariants_unchanged"] = True
        report["outside_target_value_writes"] = 0
        report["applied"] = len(audits)
        report["ok"] = True
        return report
    except Exception as error:
        if transaction_started:
            connection.rollback()
        report["rolled_back"] = apply
        report["applied"] = 0
        report["would_apply"] = 0
        report["ok"] = False
        report["failure"] = {
            "type": type(error).__name__,
            "message": str(error),
        }
        try:
            rolled_back_rows = _select_target_rows(connection, claim_ids)
            report["batch_after"] = _batch_digests(connection, claim_ids)
            for audit in report["repairs"]:
                audit["cas_applied"] = False
                audit["after"] = _row_snapshot(rolled_back_rows.get(audit["claim_id"]))
            if report["batch_before"] is not None:
                report["invariants_unchanged"] = _digests_unchanged(
                    report["batch_before"],
                    report["batch_after"],
                )
                report["outside_target_value_writes"] = (
                    0
                    if report["batch_before"]["non_target_values"] == report["batch_after"]["non_target_values"]
                    else None
                )
        except sqlite3.Error:
            pass
        return report
    finally:
        connection.close()


def _protected_database_paths(database_path: Path) -> tuple[Path, ...]:
    resolved = database_path.resolve()
    return (
        resolved,
        Path(f"{resolved}-wal"),
        Path(f"{resolved}-shm"),
        Path(f"{resolved}-journal"),
    )


def _paths_alias(left: Path, right: Path) -> bool:
    if left == right:
        return True
    if left.exists() and right.exists():
        try:
            return left.samefile(right)
        except OSError:
            return False
    return False


def write_report(
    report: dict[str, Any],
    report_path: Path,
    *,
    database_path: Path,
    manifest_path: Path,
) -> None:
    """Write an audit report without allowing protected artifacts to be overwritten."""
    resolved_report = report_path.resolve()
    protected = (*_protected_database_paths(database_path), manifest_path.resolve())
    if any(_paths_alias(resolved_report, path) for path in protected):
        raise ValueError("report path must not overwrite the database, its sidecars, or the repair manifest")
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


def _error_report(
    error: Exception,
    *,
    database_path: Path | None,
    manifest_path: Path | None,
    apply: bool,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "operation": "repair_v019_double_encoded_values",
        "audit_stage": "final",
        "mode": "apply" if apply else "dry-run",
        "database": str(database_path.resolve()) if database_path is not None else None,
        "manifest": ({"path": str(manifest_path.resolve())} if manifest_path is not None else None),
        "checked": 0,
        "applied": 0,
        "ok": False,
        "rolled_back": False,
        "failure": {
            "type": type(error).__name__,
            "message": str(error),
        },
        "repairs": [],
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = _RaisingArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--report-path", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the repair and emit one JSON audit document."""
    arguments: argparse.Namespace | None = None
    try:
        arguments = parse_args(argv)
        if arguments.apply and arguments.report_path is not None:
            write_report(
                {
                    "schema_version": 1,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "operation": "repair_v019_double_encoded_values",
                    "mode": "apply",
                    "ok": False,
                    "audit_stage": "apply_pending",
                    "database": str(arguments.database.resolve()),
                    "manifest": {"path": str(arguments.manifest.resolve())},
                },
                arguments.report_path,
                database_path=arguments.database,
                manifest_path=arguments.manifest,
            )
        report = repair_database(
            arguments.database,
            manifest_path=arguments.manifest,
            apply=arguments.apply,
        )
    except Exception as error:
        report = _error_report(
            error,
            database_path=getattr(arguments, "database", None),
            manifest_path=getattr(arguments, "manifest", None),
            apply=bool(getattr(arguments, "apply", False)),
        )

    if arguments is not None and arguments.report_path is not None:
        try:
            write_report(
                report,
                arguments.report_path,
                database_path=arguments.database,
                manifest_path=arguments.manifest,
            )
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
