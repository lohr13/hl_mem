"""Read-only structured scoring for state-coordinate lifecycle health."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hl_mem.domain.claims.conflicts import slot_qualifier_key
from hl_mem.domain.temporal import parse_utc

SCHEMA_VERSION = 1
SCORER_NAME = "structured_state_lifecycle"
_STATE_SLOT_PREFIXES = ("config.", "state.", "choice.")
_HISTORICAL_STATUSES = frozenset({"active", "archived", "superseded", "expired"})
_CLAIM_COLUMNS = (
    "id",
    "namespace_key",
    "subject_entity_id",
    "canonical_attribute",
    "canonical_slot",
    "qualifiers_json",
    "status",
    "valid_from",
    "valid_to",
    "recorded_from",
    "recorded_to",
    "supersedes_id",
    "superseded_by_id",
)
_OPTIONAL_CLAIM_COLUMNS = ("canonical_subject", "coordinate_qualifiers_json")


def open_readonly_database(database_path: str | Path) -> sqlite3.Connection:
    """Open one existing SQLite database in URI read-only and query-only mode."""

    path = Path(database_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _canonical_subject(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    return re.sub(r"\s+", "", normalized)


def _json_object(value: object) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(decoded) if isinstance(decoded, dict) else {}


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_utc(value)
    except ValueError:
        return None


def _included_at(claim: Mapping[str, Any], cutoff: datetime | None) -> bool:
    if cutoff is None:
        return True
    recorded_from = _timestamp(claim.get("recorded_from"))
    return recorded_from is not None and recorded_from <= cutoff


def _status_at(claim: Mapping[str, Any], cutoff: datetime | None) -> str:
    status = str(claim.get("status") or "active")
    if cutoff is None or status == "active":
        return status
    recorded_to = _timestamp(claim.get("recorded_to"))
    if recorded_to is not None and cutoff < recorded_to:
        return "active"
    return status


def _coordinate(claim: Mapping[str, Any], *, has_explicit_qualifiers: bool) -> dict[str, Any] | None:
    slot = str(claim.get("canonical_slot") or claim.get("canonical_attribute") or "").strip().casefold()
    if not slot.startswith(_STATE_SLOT_PREFIXES):
        return None
    raw_subject = claim.get("canonical_subject") or claim.get("subject_entity_id")
    if has_explicit_qualifiers:
        qualifiers = _json_object(claim.get("coordinate_qualifiers_json"))
    else:
        qualifiers = slot_qualifier_key(slot, _json_object(claim.get("qualifiers_json")))
    return {
        "namespace": str(claim.get("namespace_key") or ""),
        "canonical_subject": _canonical_subject(raw_subject),
        "canonical_slot": slot,
        "coordinate_qualifiers": qualifiers,
    }


def _coordinate_key(coordinate: Mapping[str, Any]) -> str:
    return json.dumps(coordinate, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _observation_key(claim: Mapping[str, Any]) -> tuple[datetime, datetime, str]:
    minimum = datetime.min.replace(tzinfo=timezone.utc)
    recorded = _timestamp(claim.get("recorded_from")) or minimum
    valid = _timestamp(claim.get("valid_from")) or recorded
    return valid, recorded, str(claim.get("id") or "")


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _historically_recallable(claim: Mapping[str, Any], reference: datetime) -> bool:
    if claim["_snapshot_status"] not in _HISTORICAL_STATUSES:
        return False
    valid_from = _timestamp(claim.get("valid_from"))
    return valid_from is None or valid_from <= reference


def _load_state_claims(
    connection: sqlite3.Connection,
    namespace: str,
    cutoff: datetime | None,
) -> tuple[list[dict[str, Any]], bool]:
    columns = _table_columns(connection, "claims")
    missing = set(_CLAIM_COLUMNS) - columns
    if missing:
        raise ValueError(f"claims table is missing required structured columns: {', '.join(sorted(missing))}")
    selected = [*_CLAIM_COLUMNS, *(name for name in _OPTIONAL_CLAIM_COLUMNS if name in columns)]
    rows = connection.execute(
        f"SELECT {','.join(selected)} FROM claims WHERE namespace_key=? ORDER BY recorded_from,id",
        (namespace,),
    ).fetchall()
    has_explicit_qualifiers = "coordinate_qualifiers_json" in columns
    claims: list[dict[str, Any]] = []
    for row in rows:
        claim = dict(row)
        if not _included_at(claim, cutoff):
            continue
        coordinate = _coordinate(claim, has_explicit_qualifiers=has_explicit_qualifiers)
        if coordinate is None:
            continue
        claim["_coordinate"] = coordinate
        claim["_snapshot_status"] = _status_at(claim, cutoff)
        claims.append(claim)
    return claims, has_explicit_qualifiers


def _edge_metrics(
    connection: sqlite3.Connection,
    claims: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], set[str]]:
    state_ids = {str(claim["id"]) for claim in claims}
    superseded_by_edges = {
        (str(claim["id"]), str(claim["superseded_by_id"]))
        for claim in claims
        if claim.get("superseded_by_id") and str(claim["superseded_by_id"]) in state_ids
    }
    supersedes_edges = {
        (str(claim["supersedes_id"]), str(claim["id"]))
        for claim in claims
        if claim.get("supersedes_id") and str(claim["supersedes_id"]) in state_ids
    }
    evidence_edges = {
        (str(row["evidence_id"]), str(row["derived_id"]))
        for row in connection.execute(
            "SELECT derived_id,evidence_id FROM evidence_links "
            "WHERE relation='supersedes' AND derived_type='claim' AND evidence_type='claim'"
        )
        if str(row["derived_id"]) in state_ids and str(row["evidence_id"]) in state_ids
    }
    edges = superseded_by_edges | supersedes_edges | evidence_edges
    return (
        {
            "total": len(edges),
            "sources": {
                "claims.superseded_by_id": len(superseded_by_edges),
                "claims.supersedes_id": len(supersedes_edges),
                "evidence_links": len(evidence_edges),
            },
        },
        {old_id for old_id, _new_id in edges},
    )


def _audit_row_count(connection: sqlite3.Connection, namespace: str) -> int:
    columns = _table_columns(connection, "audit_log")
    if not columns:
        return 0
    if "tenant_id" in columns:
        row = connection.execute("SELECT count(*) FROM audit_log WHERE tenant_id=?", (namespace,)).fetchone()
    else:
        row = connection.execute("SELECT count(*) FROM audit_log").fetchone()
    return int(row[0]) if row is not None else 0


def _score_connection(
    connection: sqlite3.Connection,
    *,
    namespace: str,
    recorded_at: str | None,
) -> dict[str, Any]:
    cutoff = parse_utc(recorded_at) if recorded_at is not None else None
    claims, has_explicit_qualifiers = _load_state_claims(connection, namespace, cutoff)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for claim in claims:
        grouped[_coordinate_key(claim["_coordinate"])].append(claim)

    groups: list[dict[str, Any]] = []
    covered_old_ids: set[str] = set()
    for members in grouped.values():
        ordered = sorted(members, key=_observation_key)
        if len(ordered) > 1:
            covered_old_ids.update(str(claim["id"]) for claim in ordered[:-1])
        active_count = sum(claim["_snapshot_status"] == "active" for claim in members)
        health = "healthy" if active_count == 1 else "drifted" if active_count > 1 else "inactive"
        groups.append(
            {
                "coordinate": dict(members[0]["_coordinate"]),
                "claim_count": len(members),
                "active_count": active_count,
                "health": health,
            }
        )
    groups.sort(key=lambda item: _coordinate_key(item["coordinate"]))
    summary = {
        "total": len(groups),
        "healthy": sum(group["health"] == "healthy" for group in groups),
        "drifted": sum(group["health"] == "drifted" for group in groups),
        "inactive": sum(group["health"] == "inactive" for group in groups),
    }

    edge_metrics, closed_chain_candidates = _edge_metrics(connection, claims)
    claims_by_id = {str(claim["id"]): claim for claim in claims}
    closed = sum(bool(claims_by_id[claim_id].get("valid_to")) for claim_id in closed_chain_candidates)
    active_claims = [claim for claim in claims if claim["_snapshot_status"] == "active"]
    stale_active = sum(str(claim["id"]) in covered_old_ids for claim in active_claims)
    reference = cutoff or datetime.now(timezone.utc)
    historical_candidates = [claims_by_id[claim_id] for claim_id in covered_old_ids]
    historical_recallable = sum(_historically_recallable(claim, reference) for claim in historical_candidates)
    invalid_timestamps = sum(
        value is not None and _timestamp(value) is None
        for claim in claims
        for value in (
            claim.get("valid_from"),
            claim.get("valid_to"),
            claim.get("recorded_from"),
            claim.get("recorded_to"),
        )
    )

    return {
        "namespace": namespace,
        "recorded_at": recorded_at,
        "coordinate_groups": {"summary": summary, "groups": groups},
        "supersede_edges": edge_metrics,
        "valid_to_closure": {
            "eligible": len(closed_chain_candidates),
            "closed": closed,
            "rate": _rate(closed, len(closed_chain_candidates)),
        },
        "current_state_stale_injection": {
            "active_surface": len(active_claims),
            "stale_active": stale_active,
            "rate": _rate(stale_active, len(active_claims)),
        },
        "historical_old_snapshot_recall": {
            "covered_old": len(historical_candidates),
            "recallable": historical_recallable,
            "rate": _rate(historical_recallable, len(historical_candidates)),
        },
        "diagnostics": {
            "audit_rows": _audit_row_count(connection, namespace),
            "coordinate_qualifier_source": (
                "coordinate_qualifiers_json" if has_explicit_qualifiers else "slot_required_qualifiers"
            ),
            "invalid_timestamps": invalid_timestamps,
        },
    }


def score_database(
    database_path: str | Path,
    *,
    namespace: str = "default",
    recorded_at: str | None = None,
) -> dict[str, Any]:
    """Score one database snapshot without running migrations or issuing writes."""

    path = Path(database_path).resolve()
    connection = open_readonly_database(path)
    try:
        connection.execute("BEGIN")
        result = _score_connection(connection, namespace=namespace, recorded_at=recorded_at)
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    return {"database": str(path), **result}


def _delta(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, int | float]:
    paths = {
        "coordinate_groups.total": ("coordinate_groups", "summary", "total"),
        "coordinate_groups.healthy": ("coordinate_groups", "summary", "healthy"),
        "coordinate_groups.drifted": ("coordinate_groups", "summary", "drifted"),
        "supersede_edges.total": ("supersede_edges", "total"),
        "valid_to_closure.rate": ("valid_to_closure", "rate"),
        "current_state_stale_injection.rate": ("current_state_stale_injection", "rate"),
        "historical_old_snapshot_recall.rate": ("historical_old_snapshot_recall", "rate"),
    }
    result: dict[str, int | float] = {}
    for name, path in paths.items():
        before_value: Any = before
        after_value: Any = after
        for part in path:
            before_value = before_value[part]
            after_value = after_value[part]
        result[name] = after_value - before_value
    return result


def _comparison_report(
    mode: str,
    namespace: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "scorer": SCORER_NAME,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "namespace": namespace,
        "snapshots": {"before": dict(before), "after": dict(after)},
        "delta": _delta(before, after),
    }


def compare_database_snapshots(
    before_database: str | Path,
    after_database: str | Path,
    *,
    namespace: str = "default",
) -> dict[str, Any]:
    """Compare two independently persisted database snapshots."""

    before = score_database(before_database, namespace=namespace)
    after = score_database(after_database, namespace=namespace)
    return _comparison_report("database_comparison", namespace, before, after)


def compare_database_interval(
    database_path: str | Path,
    *,
    before_at: str,
    after_at: str,
    namespace: str = "default",
) -> dict[str, Any]:
    """Compare two recorded-time cutoffs inferred from one structured database."""

    if parse_utc(after_at) <= parse_utc(before_at):
        raise ValueError("after_at must be later than before_at")
    before = score_database(database_path, namespace=namespace, recorded_at=before_at)
    after = score_database(database_path, namespace=namespace, recorded_at=after_at)
    return _comparison_report("interval_comparison", namespace, before, after)


def _single_report(snapshot: Mapping[str, Any], namespace: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "scorer": SCORER_NAME,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "single",
        "namespace": namespace,
        "snapshots": {"current": dict(snapshot)},
    }


def _summary(report: Mapping[str, Any], output: Path) -> dict[str, Any]:
    snapshot_name = "current" if report["mode"] == "single" else "after"
    snapshot = report["snapshots"][snapshot_name]
    groups = snapshot["coordinate_groups"]["summary"]
    return {
        "coordinate_groups": groups["total"],
        "drifted_groups": groups["drifted"],
        "healthy_groups": groups["healthy"],
        "mode": report["mode"],
        "output": str(output),
        "supersede_edges": snapshot["supersede_edges"]["total"],
    }


def _parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="只读评估结构化状态坐标与 supersede 生命周期")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--before-db", type=Path)
    parser.add_argument("--after-db", type=Path)
    parser.add_argument("--before-at")
    parser.add_argument("--after-at")
    parser.add_argument("--recorded-at")
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    comparing_databases = arguments.before_db is not None or arguments.after_db is not None
    comparing_interval = arguments.before_at is not None or arguments.after_at is not None
    if comparing_databases:
        if arguments.before_db is None or arguments.after_db is None:
            parser.error("--before-db 与 --after-db 必须同时提供")
        if arguments.db is not None or comparing_interval or arguments.recorded_at is not None:
            parser.error("两库比较不能与 --db、时间区间或 --recorded-at 混用")
    elif comparing_interval:
        if arguments.db is None or arguments.before_at is None or arguments.after_at is None:
            parser.error("同库区间比较需要 --db、--before-at 与 --after-at")
        if arguments.recorded_at is not None:
            parser.error("区间比较不能与 --recorded-at 混用")
    elif arguments.db is None:
        parser.error("单库评分需要 --db")
    return arguments


def main(argv: Sequence[str] | None = None) -> int:
    """Run the scorer and persist a stable JSON report."""

    arguments = _parse_arguments(argv)
    if arguments.before_db is not None:
        report = compare_database_snapshots(
            arguments.before_db,
            arguments.after_db,
            namespace=arguments.namespace,
        )
    elif arguments.before_at is not None:
        report = compare_database_interval(
            arguments.db,
            before_at=arguments.before_at,
            after_at=arguments.after_at,
            namespace=arguments.namespace,
        )
    else:
        snapshot = score_database(
            arguments.db,
            namespace=arguments.namespace,
            recorded_at=arguments.recorded_at,
        )
        report = _single_report(snapshot, arguments.namespace)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(_summary(report, arguments.output), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
