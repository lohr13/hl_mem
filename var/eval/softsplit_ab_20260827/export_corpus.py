"""Export the frozen compact==20 corpus manifest without copying message bodies."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROTOCOL_ID = "softsplit_ab_20260827_v1"
DEFAULT_SINCE = "2026-08-19T00:00:00+00:00"
DEFAULT_EXPECTED_CASES = 83
EQUIPMENT_DIR = Path(__file__).resolve().parent
DEFAULT_DATABASE = EQUIPMENT_DIR.parents[2] / "var/hl_mem.db"
DEFAULT_OUTPUT = EQUIPMENT_DIR / "manifest.json"


def _open_read_only(database_path: Path) -> sqlite3.Connection:
    resolved = database_path.resolve()
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _source_event_ids(
    connection: sqlite3.Connection,
    *,
    trace_id: str,
    audit_id: int,
    fallback_event_id: str,
) -> list[str]:
    rows = connection.execute(
        "SELECT detail_json FROM audit_log "
        "WHERE trace_id=? AND id>=? AND phase='extraction' AND action='evaluated' "
        "ORDER BY id LIMIT 5",
        (trace_id, audit_id),
    ).fetchall()
    for row in rows:
        try:
            detail = json.loads(str(row["detail_json"]))
        except (TypeError, ValueError):
            continue
        source_ids = detail.get("source_event_ids") if isinstance(detail, dict) else None
        if isinstance(source_ids, list) and source_ids and all(isinstance(item, str) for item in source_ids):
            return list(dict.fromkeys(source_ids))
    return [fallback_event_id]


def _source_snapshots(connection: sqlite3.Connection, event_ids: list[str]) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for event_id in event_ids:
        row = connection.execute(
            "SELECT id,content_json,content_hash FROM events WHERE id=?",
            (event_id,),
        ).fetchone()
        if row is None:
            snapshots.append(
                {
                    "event_id": event_id,
                    "available": False,
                    "content_sha256": None,
                    "stored_content_hash": None,
                }
            )
            continue
        content_json = str(row["content_json"])
        snapshots.append(
            {
                "event_id": str(row["id"]),
                "available": True,
                "content_sha256": hashlib.sha256(content_json.encode("utf-8")).hexdigest(),
                "stored_content_hash": str(row["content_hash"] or ""),
            }
        )
    return snapshots


def export_manifest(
    database_path: Path,
    output_path: Path,
    *,
    since: str = DEFAULT_SINCE,
    expected_cases: int | None = DEFAULT_EXPECTED_CASES,
    exported_at: str | None = None,
) -> dict[str, Any]:
    """Lock the saturated case IDs and source hashes in a body-free manifest."""
    if expected_cases is not None and expected_cases < 1:
        raise ValueError("expected_cases must be positive or omitted")
    exported_at = exported_at or datetime.now(timezone.utc).isoformat()
    with _open_read_only(database_path) as connection:
        rows = connection.execute(
            "SELECT MIN(id) AS audit_id,event_id,trace_id,MIN(occurred_at) AS occurred_at "
            "FROM audit_log "
            "WHERE datetime(occurred_at)>=datetime(?) "
            "AND phase='extract' AND action='possible_under_extraction' "
            "AND outcome='claim_limit_reached' AND event_id IS NOT NULL "
            "GROUP BY event_id,trace_id ORDER BY MIN(occurred_at),event_id",
            (since,),
        ).fetchall()
        cases: list[dict[str, Any]] = []
        seen_case_ids: set[str] = set()
        for row in rows:
            event_id = str(row["event_id"])
            if event_id in seen_case_ids:
                continue
            seen_case_ids.add(event_id)
            trace_id = str(row["trace_id"])
            audit_id = int(row["audit_id"])
            source_ids = _source_event_ids(
                connection,
                trace_id=trace_id,
                audit_id=audit_id,
                fallback_event_id=event_id,
            )
            sources = _source_snapshots(connection, source_ids)
            cases.append(
                {
                    "case_id": event_id,
                    "audit_trace_id": trace_id,
                    "claim_limit_audit_id": audit_id,
                    "claim_limit_occurred_at": str(row["occurred_at"]),
                    "source_event_ids": source_ids,
                    "unavailable_source_event_ids": [
                        str(source["event_id"]) for source in sources if not source["available"]
                    ],
                    "sources": sources,
                }
            )

    if expected_cases is not None and len(cases) != expected_cases:
        raise ValueError(f"expected {expected_cases} cases, found {len(cases)}")
    manifest = {
        "protocol_id": PROTOCOL_ID,
        "exported_at": exported_at,
        "source_database": str(database_path.resolve()),
        "selection": {
            "since": since,
            "phase": "extract",
            "action": "possible_under_extraction",
            "outcome": "claim_limit_reached",
        },
        "case_count": len(cases),
        "contains_message_bodies": False,
        "cases": cases,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--since", default=DEFAULT_SINCE)
    parser.add_argument("--expected-cases", type=int, default=DEFAULT_EXPECTED_CASES)
    return parser


def main() -> int:
    args = _parser().parse_args()
    manifest = export_manifest(
        args.database,
        args.output,
        since=args.since,
        expected_cases=args.expected_cases,
    )
    print(json.dumps({"output": str(args.output), "case_count": manifest["case_count"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
