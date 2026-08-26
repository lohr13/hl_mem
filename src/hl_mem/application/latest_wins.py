import json
import sqlite3
from dataclasses import astuple
from typing import Any, NamedTuple, Sequence

from hl_mem import state_latest_wins as latest
from hl_mem.application.ingest_evidence import link_source_events
from hl_mem.domain.claims.state_coordinates import StateCoordinate
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.evidence import EvidenceRepository

_AUTHORITY = {"low": 1, "medium": 2, "high": 3}
_ACTIONABLE = {"duplicate", "corroborates", "supersedes_existing", "historical_predecessor"}
AuditEvent = tuple[tuple[Any, ...], dict[str, Any]]


class LatestWinsPlan(NamedTuple):
    existing: dict[str, Any]
    resolution: latest.TemporalResolution

    def audit_event(self, mode: str, incoming_id: str, *, applied: bool = False) -> AuditEvent:
        detail = {
            "schema_version": "state_latest_wins_audit_v1",
            "mode": mode,
            "rule_id": self.resolution.rule_id,
            "reason": self.resolution.reason,
        }
        return (
            ("state_latest_wins", "applied" if applied else "suggested", self.resolution.relation),
            {"claim_id": self.existing["id"], "related_claim_id": incoming_id, "detail": detail},
        )


def begin_latest_wins(
    connection: sqlite3.Connection,
    incoming: dict[str, Any],
    source_events: Sequence[dict[str, Any]],
    proof: latest.CurrentnessProof | None,
    audit_events: list[AuditEvent],
) -> tuple[LatestWinsPlan | None, tuple[str, str] | None]:
    configured = getattr(connection, "hl_mem_settings", Settings())
    mode = configured.latest_wins_mode
    plan = prepare_latest_wins(
        connection, incoming, source_events, proof, mode=mode, slots=configured.latest_wins_slots
    )
    if plan is None:
        return None, None
    relation = plan.resolution.relation
    actionable = mode == "enforce" and relation in _ACTIONABLE
    if not actionable:
        audit_events.append(plan.audit_event(mode, str(incoming["id"])))
    if actionable and relation in {"duplicate", "corroborates"}:
        claim_id = str(plan.existing["id"])
        link_source_events(EvidenceRepository(connection), claim_id, source_events)
        audit_events.append(plan.audit_event(mode, str(incoming["id"]), applied=True))
        return plan, (claim_id, f"latest_wins_{relation}")
    return plan, None


def finish_latest_wins(
    plan: LatestWinsPlan | None,
    claims: ClaimRepository,
    incoming: dict[str, Any],
    now: str,
    audit_events: list[AuditEvent],
) -> None:
    mode = getattr(claims.connection, "hl_mem_settings", Settings()).latest_wins_mode
    if plan is None or mode != "enforce":
        return
    if plan.resolution.relation in {"supersedes_existing", "historical_predecessor"}:
        old, new = (
            (incoming, plan.existing)
            if plan.resolution.relation == "historical_predecessor"
            else (plan.existing, incoming)
        )
        if not claims.supersede_with_inline(
            str(old["id"]), str(new["id"]), new.get("value"), str(new["valid_from"]), now, commit=False
        ).applied:
            raise RuntimeError("latest-wins compare-and-set failed")
        audit_events.append(plan.audit_event(mode, str(incoming["id"]), applied=True))


def prepare_latest_wins(
    connection: sqlite3.Connection,
    incoming: dict[str, Any],
    source_events: Sequence[dict[str, Any]],
    proof: latest.CurrentnessProof | None,
    *,
    mode: str,
    slots: tuple[str, ...],
) -> LatestWinsPlan | None:
    if mode == "off" or incoming.get("canonical_slot") != "config.version" or "config.version" not in slots:
        return None
    subject = incoming.get("subject_canonical_entity_id") or incoming.get("subject_entity_id")
    coordinate = StateCoordinate(
        str(incoming["namespace_key"]), str(subject), "config.version", incoming.get("qualifiers") or {}
    )
    incoming["conflict_key"] = json.dumps(astuple(coordinate), ensure_ascii=False, separators=(",", ":"))
    rows = connection.execute(
        "SELECT id FROM claims WHERE conflict_key=? AND namespace_key=? AND canonical_slot=? "
        "AND COALESCE(subject_canonical_entity_id,subject_entity_id)=? "
        "AND json(qualifiers_json)=json(?) AND status IN ('active','candidate','disputed') "
        "ORDER BY valid_from DESC,recorded_from DESC,id DESC LIMIT 17",
        (
            incoming.get("conflict_key"),
            incoming.get("namespace_key"),
            incoming.get("canonical_slot"),
            subject,
            incoming.get("qualifiers_json", "{}"),
        ),
    ).fetchall()
    candidates = [ClaimRepository(connection).get_claim(str(row[0])) for row in rows]
    existing = next((claim for claim in candidates if claim is not None), None)
    if existing is None:
        return None
    chain = connection.execute(
        "WITH RECURSIVE c(id,path,cycle,depth) AS (SELECT ?,','||?||',',0,0 UNION ALL "
        "SELECT q.id,c.path||q.id||',',instr(c.path,','||q.id||','),c.depth+1 FROM claims q JOIN c "
        "ON q.superseded_by_id=c.id WHERE c.cycle=0 AND c.depth<64) SELECT max(cycle),max(depth) FROM c",
        (existing["id"], existing["id"]),
    ).fetchone()
    tip = latest.CurrentTipState(
        len(candidates),
        str(existing["status"]),
        bool(chain and int(chain[0] or 0) == 0 and int(chain[1] or 0) < 64),
        len(candidates) < 17,
    )
    return LatestWinsPlan(
        existing,
        latest.resolve_latest_wins(
            _version_claim(connection, existing, coordinate),
            _version_claim(connection, incoming, coordinate, source_events),
            proof if len(source_events) == 1 else None,
            tip,
        ),
    )


def _version_claim(
    connection: sqlite3.Connection,
    claim: dict[str, Any],
    coordinate: StateCoordinate,
    source_events: Sequence[dict[str, Any]] = (),
) -> latest.VersionClaim:
    if source_events:
        evidence_id = str(source_events[0]["id"])
        row = connection.execute(
            "SELECT 1 FROM events WHERE id=? AND tenant_id=? AND occurred_at=?",
            (evidence_id, coordinate.namespace, claim["observed_at"]),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT link.evidence_id FROM evidence_links link JOIN events event ON event.id=link.evidence_id "
            "WHERE link.derived_type='claim' AND link.derived_id=? AND link.evidence_type='event' "
            "AND link.relation IN ('derived_from','supports') AND event.tenant_id=? AND event.occurred_at=? LIMIT 1",
            (claim["id"], coordinate.namespace, claim["observed_at"]),
        ).fetchone()
        evidence_id = str(row[0]) if row else ""
    value = claim.get("value")
    return latest.VersionClaim(
        str(claim["id"]),
        coordinate,
        value if isinstance(value, str) else "",
        claim.get("observed_at"),
        _AUTHORITY.get(str(claim.get("source_authority")), 0),
        evidence_id,
        row is not None,
        row is not None,
        assertion_kind=str(claim.get("assertion_kind") or "unknown"),
        atomic_value=isinstance(value, str),
    )
