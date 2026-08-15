"""Read-only bounded reporting for dangling database references."""

from __future__ import annotations

from typing import Any

DANGLING_SAMPLE_LIMIT = 5

_EVIDENCE_FROM = """
FROM evidence_links AS link
LEFT JOIN claims AS derived_claim
  ON link.derived_type='claim' AND derived_claim.id=link.derived_id
LEFT JOIN claims AS evidence_claim
  ON link.evidence_type='claim' AND evidence_claim.id=link.evidence_id
LEFT JOIN events AS evidence_event
  ON link.evidence_type='event' AND evidence_event.id=link.evidence_id
WHERE link.derived_type<>'observation'
  AND (
    link.derived_type<>'claim'
    OR derived_claim.id IS NULL
    OR (link.evidence_type='claim' AND evidence_claim.id IS NULL)
    OR (link.evidence_type='event' AND evidence_event.id IS NULL)
    OR link.evidence_type NOT IN ('claim','event')
  )
"""

_RELATION_FROM = """
FROM memory_relations AS relation
LEFT JOIN claims AS from_claim ON from_claim.id=relation.from_id
LEFT JOIN claims AS to_claim ON to_claim.id=relation.to_id
WHERE from_claim.id IS NULL OR to_claim.id IS NULL
"""

_DERIVATION_SUPERSEDE_ROWS = """
SELECT 'derivation' AS kind,
       link.id AS id,
       link.derived_id AS source_id,
       link.evidence_id AS target_id,
       derivation.id AS source_exists,
       CASE
         WHEN link.evidence_type='claim' THEN evidence_claim.id
         WHEN link.evidence_type='event' THEN evidence_event.id
       END AS target_exists
FROM evidence_links AS link
LEFT JOIN derivations AS derivation
  ON derivation.id=link.derived_id
LEFT JOIN claims AS evidence_claim
  ON link.evidence_type='claim' AND evidence_claim.id=link.evidence_id
LEFT JOIN events AS evidence_event
  ON link.evidence_type='event' AND evidence_event.id=link.evidence_id
WHERE link.derived_type='observation'
  AND (
    derivation.id IS NULL
    OR (link.evidence_type='claim' AND evidence_claim.id IS NULL)
    OR (link.evidence_type='event' AND evidence_event.id IS NULL)
    OR link.evidence_type NOT IN ('claim','event')
  )
UNION ALL
SELECT 'superseded_by_id' AS kind,
       source.id AS id,
       source.id AS source_id,
       source.superseded_by_id AS target_id,
       source.id AS source_exists,
       target.id AS target_exists
FROM claims AS source
LEFT JOIN claims AS target ON target.id=source.superseded_by_id
WHERE source.superseded_by_id IS NOT NULL AND target.id IS NULL
UNION ALL
SELECT 'supersedes_id' AS kind,
       source.id AS id,
       source.id AS source_id,
       source.supersedes_id AS target_id,
       source.id AS source_exists,
       target.id AS target_exists
FROM claims AS source
LEFT JOIN claims AS target ON target.id=source.supersedes_id
WHERE source.supersedes_id IS NOT NULL AND target.id IS NULL
"""


def _count(connection: Any, from_sql: str) -> int:
    return int(connection.execute(f"SELECT count(*) {from_sql}").fetchone()[0])


def _evidence_report(connection: Any) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT link.id,link.derived_type,link.derived_id,link.evidence_type,link.evidence_id,"
        "derived_claim.id AS derived_exists,evidence_claim.id AS evidence_claim_exists,"
        f"evidence_event.id AS evidence_event_exists {_EVIDENCE_FROM} "
        "ORDER BY link.id LIMIT ?",
        (DANGLING_SAMPLE_LIMIT,),
    ).fetchall()
    samples = []
    for row in rows:
        missing = []
        if str(row["derived_type"]) != "claim" or row["derived_exists"] is None:
            missing.append("derived")
        evidence_type = str(row["evidence_type"])
        if (
            evidence_type not in {"claim", "event"}
            or (evidence_type == "claim" and row["evidence_claim_exists"] is None)
            or (evidence_type == "event" and row["evidence_event_exists"] is None)
        ):
            missing.append("evidence")
        samples.append(
            {
                "id": str(row["id"]),
                "derived_type": str(row["derived_type"]),
                "derived_id": str(row["derived_id"]),
                "evidence_type": evidence_type,
                "evidence_id": str(row["evidence_id"]),
                "missing": missing,
            }
        )
    return {"count": _count(connection, _EVIDENCE_FROM), "samples": samples}


def _relation_report(connection: Any) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT relation.id,relation.from_id,relation.to_id,"
        f"from_claim.id AS from_exists,to_claim.id AS to_exists {_RELATION_FROM} "
        "ORDER BY relation.id LIMIT ?",
        (DANGLING_SAMPLE_LIMIT,),
    ).fetchall()
    samples = [
        {
            "id": str(row["id"]),
            "from_id": str(row["from_id"]),
            "to_id": str(row["to_id"]),
            "missing": [
                endpoint
                for endpoint, exists in (("from", row["from_exists"]), ("to", row["to_exists"]))
                if exists is None
            ],
        }
        for row in rows
    ]
    return {"count": _count(connection, _RELATION_FROM), "samples": samples}


def _derivation_supersede_report(connection: Any) -> dict[str, Any]:
    count = int(connection.execute(f"SELECT count(*) FROM ({_DERIVATION_SUPERSEDE_ROWS}) AS dangling").fetchone()[0])
    rows = connection.execute(
        f"SELECT * FROM ({_DERIVATION_SUPERSEDE_ROWS}) AS dangling " "ORDER BY kind,id LIMIT ?",
        (DANGLING_SAMPLE_LIMIT,),
    ).fetchall()
    samples = []
    for row in rows:
        kind = str(row["kind"])
        missing = []
        if row["source_exists"] is None:
            missing.append("derivation" if kind == "derivation" else "source")
        if row["target_exists"] is None:
            missing.append("evidence" if kind == "derivation" else "target")
        samples.append(
            {
                "kind": kind,
                "id": str(row["id"]),
                "source_id": str(row["source_id"]),
                "target_id": str(row["target_id"]),
                "missing": missing,
            }
        )
    return {"count": count, "samples": samples}


def audit_dangling_references(connection: Any) -> dict[str, Any]:
    """Count dangling references and return at most five identifier-only samples per class."""
    evidence = _evidence_report(connection)
    relations = _relation_report(connection)
    derivation_supersede = _derivation_supersede_report(connection)
    return {
        "total_count": evidence["count"] + relations["count"] + derivation_supersede["count"],
        "evidence_links": evidence,
        "relation_endpoints": relations,
        "derivation_supersede_references": derivation_supersede,
    }
