"""冲突 supersession 快照、案卷与指纹的共享只读边界。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from hl_mem.domain.claims.attributes import is_mutually_exclusive_attribute
from hl_mem.domain.claims.conflicts import coordinate_qualifier_key
from hl_mem.domain.governance import is_terminal_conflict_status
from hl_mem.errors import ConflictResolutionError
from hl_mem.storage.claims import ClaimRepository

MAX_SUPERSESSION_DEPTH = 32
MAX_EVIDENCE_PER_CLAIM = 5
MAX_EVENT_CONTENT_CHARS = 500
_CASE_FINGERPRINT_FIELDS = (
    "id",
    "pair_key",
    "left_claim_id",
    "right_claim_id",
    "namespace_key",
    "group_key",
    "generation",
    "revision",
    "status",
    "decision",
    "rationale",
    "resolved_at",
    "overflow",
    "policy_version",
    "last_tier",
    "resolution_rule",
    "resolver_model",
)
_CLAIM_FINGERPRINT_FIELDS = (
    "id",
    "status",
    "namespace_key",
    "subject_entity_id",
    "canonical_slot",
    "value",
    "qualifiers",
    "valid_from",
    "valid_to",
    "recorded_from",
    "recorded_to",
    "observed_at",
    "source_authority",
    "confidence",
    "assertion_kind",
    "superseded_by_id",
)


class StaleConflictDecision(ConflictResolutionError):
    """裁决期间 case revision 或 v2 输入指纹已经变化。"""


@dataclass(frozen=True)
class ClaimLineage:
    """从 case 端点到当前 tip 的有序 supersession 链。"""

    claims: tuple[dict[str, Any], ...]
    edges: tuple[tuple[str, str], ...]

    @property
    def tip(self) -> dict[str, Any]:
        return self.claims[-1]

    @property
    def tip_id(self) -> str:
        return str(self.tip["id"])

    def snapshot(self) -> dict[str, Any]:
        return {
            "start_id": str(self.claims[0]["id"]),
            "tip_id": self.tip_id,
            "claims": list(self.claims),
            "edges": [{"source_id": source_id, "target_id": target_id} for source_id, target_id in self.edges],
        }


def resolve_claim_lineage(repository: ClaimRepository, claim_id: str) -> ClaimLineage:
    return resolve_claim_lineages(repository, [claim_id])[claim_id]


def resolve_claim_lineages(
    repository: ClaimRepository,
    claim_ids: list[str],
) -> dict[str, ClaimLineage]:
    """按深度批量展开多条 lineage，避免 group dossier 对成员逐条回表。"""

    starts = list(dict.fromkeys(claim_ids))
    current = {start_id: start_id for start_id in starts}
    visited: dict[str, set[str]] = {start_id: set() for start_id in starts}
    claims: dict[str, list[dict[str, Any]]] = {start_id: [] for start_id in starts}
    edges: dict[str, list[tuple[str, str]]] = {start_id: [] for start_id in starts}
    results: dict[str, ClaimLineage] = {}
    while current:
        loaded = repository.batch_get_claims(list(dict.fromkeys(current.values())))
        for start_id, current_id in list(current.items()):
            if current_id in visited[start_id]:
                raise ConflictResolutionError(f"supersession cycle in conflict lineage: {current_id}")
            visited[start_id].add(current_id)
            claim = loaded.get(current_id)
            if claim is None:
                raise ConflictResolutionError(f"supersession claim is missing: {current_id}")
            claims[start_id].append(claim)
            successor = claim.get("superseded_by_id")
            if not successor:
                if claim.get("status") == "superseded":
                    raise ConflictResolutionError(f"superseded claim has no successor: {current_id}")
                results[start_id] = ClaimLineage(
                    tuple(claims[start_id]),
                    tuple(edges[start_id]),
                )
                del current[start_id]
                continue
            if len(edges[start_id]) >= MAX_SUPERSESSION_DEPTH:
                raise ConflictResolutionError(f"supersession depth exceeds {MAX_SUPERSESSION_DEPTH}: {start_id}")
            successor_id = str(successor)
            edges[start_id].append((current_id, successor_id))
            current[start_id] = successor_id
    return results


def _load_evidence(
    connection: sqlite3.Connection,
    claim_ids: list[str],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    placeholders = ",".join("?" for _ in claim_ids)
    rows = connection.execute(
        "WITH ranked AS (SELECT links.derived_id,links.id,links.evidence_type,"
        "links.evidence_id,links.relation,links.weight,events.event_type,events.occurred_at,"
        "events.content_json,ROW_NUMBER() OVER (PARTITION BY links.derived_id ORDER BY links.id) "
        "AS evidence_rank,COUNT(*) OVER (PARTITION BY links.derived_id) AS evidence_count "
        "FROM evidence_links AS links LEFT JOIN events "
        "ON links.evidence_type='event' AND events.id=links.evidence_id "
        f"WHERE links.derived_type='claim' AND links.derived_id IN ({placeholders})) "
        "SELECT * FROM ranked WHERE evidence_rank<=? ORDER BY derived_id,id",
        (*claim_ids, MAX_EVIDENCE_PER_CLAIM),
    ).fetchall()
    counts = {claim_id: 0 for claim_id in claim_ids}
    evidence: list[dict[str, Any]] = []
    for row in rows:
        counts[str(row["derived_id"])] = int(row["evidence_count"])
        evidence.append(
            {
                "derived_id": str(row["derived_id"]),
                "id": str(row["id"]),
                "evidence_type": str(row["evidence_type"]),
                "evidence_id": str(row["evidence_id"]),
                "relation": str(row["relation"]),
                "weight": row["weight"],
                "event_type": row["event_type"],
                "occurred_at": row["occurred_at"],
                "content_json": (
                    str(row["content_json"])[:MAX_EVENT_CONTENT_CHARS] if row["content_json"] is not None else None
                ),
            }
        )
    return evidence, counts


def _load_candidates(
    connection: sqlite3.Connection,
    case_id: str,
    lineage_roots: list[str],
) -> tuple[list[dict[str, Any]], dict[str, ClaimLineage]]:
    rows = connection.execute(
        "SELECT candidates.candidate_key,candidates.canonical_value_json,"
        "candidates.representative_claim_id,candidates.support_count,candidates.first_seen_at,"
        "candidates.last_seen_at,members.claim_id AS member_claim_id,claims.id AS loaded_claim_id,"
        "claims.status AS member_status,claims.source_authority AS member_source_authority,"
        "(SELECT count(*) FROM evidence_links AS links WHERE links.derived_type='claim' "
        "AND links.derived_id=members.claim_id) AS member_evidence_count "
        "FROM conflict_case_candidates AS candidates "
        "LEFT JOIN conflict_candidate_members AS members ON members.case_id=candidates.case_id "
        "AND members.candidate_key=candidates.candidate_key "
        "LEFT JOIN claims ON claims.id=members.claim_id "
        "WHERE candidates.case_id=? ORDER BY candidates.candidate_key,members.claim_id",
        (case_id,),
    ).fetchall()
    candidate_rows: dict[str, sqlite3.Row] = {}
    members_by_candidate: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        candidate_key = str(row["candidate_key"])
        candidate_rows.setdefault(candidate_key, row)
        member_id = row["member_claim_id"]
        if member_id is None:
            continue
        if row["loaded_claim_id"] is None:
            raise ConflictResolutionError(f"conflict candidate member is missing: {member_id}")
        members_by_candidate.setdefault(candidate_key, []).append(
            {
                "claim_id": str(member_id),
                "status": str(row["member_status"]),
                "source_authority": row["member_source_authority"],
                "evidence_count": int(row["member_evidence_count"]),
            }
        )
    roots = list(lineage_roots)
    roots.extend(str(row["representative_claim_id"]) for row in candidate_rows.values())
    roots.extend(str(member["claim_id"]) for members in members_by_candidate.values() for member in members)
    repository = ClaimRepository(connection)
    lineages = resolve_claim_lineages(repository, roots)
    candidates: list[dict[str, Any]] = []
    for candidate_key, row in candidate_rows.items():
        candidate_members = members_by_candidate.get(candidate_key, [])
        if int(row["support_count"]) != len(candidate_members):
            raise ConflictResolutionError(
                f"conflict candidate support count is inconsistent: {case_id}:{candidate_key}"
            )
        claim_ids = [str(member["claim_id"]) for member in candidate_members]
        evidence_count = sum(int(member["evidence_count"]) for member in candidate_members)
        member_lineages = {member_id: lineages[member_id] for member_id in claim_ids}
        representative = lineages[str(row["representative_claim_id"])]
        member_tips = [lineage.tip for lineage in member_lineages.values()]
        candidates.append(
            {
                "candidate_key": candidate_key,
                "canonical_value_json": str(row["canonical_value_json"]),
                "representative_claim_id": str(row["representative_claim_id"]),
                "representative_tip_id": representative.tip_id,
                "support_count": int(row["support_count"]),
                "evidence_count": evidence_count,
                "first_seen_at": str(row["first_seen_at"]),
                "last_seen_at": str(row["last_seen_at"]),
                "claim_ids": claim_ids,
                "claim_statuses": {str(member["claim_id"]): str(member["status"]) for member in candidate_members},
                "member_lineages": {member_id: lineage.snapshot() for member_id, lineage in member_lineages.items()},
                "terminal": bool(member_tips)
                and all(is_terminal_conflict_status(tip.get("status")) for tip in member_tips),
            }
        )
    return candidates, lineages


def _coordinates_complete(left: dict[str, Any], right: dict[str, Any]) -> bool:
    slot = left.get("canonical_slot")
    return bool(
        isinstance(slot, str)
        and slot == right.get("canonical_slot")
        and left.get("namespace_key") == right.get("namespace_key")
        and left.get("subject_entity_id")
        and left.get("subject_entity_id") == right.get("subject_entity_id")
        and coordinate_qualifier_key(slot, left.get("qualifiers"))
        == coordinate_qualifier_key(slot, right.get("qualifiers"))
    )


def _is_exclusive_group(left: dict[str, Any], right: dict[str, Any]) -> bool:
    conflict_key = left.get("conflict_key")
    return bool(
        conflict_key
        and conflict_key == right.get("conflict_key")
        and left.get("namespace_key") == right.get("namespace_key")
        and is_mutually_exclusive_attribute(left.get("canonical_slot"))
        and is_mutually_exclusive_attribute(right.get("canonical_slot"))
    )


def _load_resolution_scope(
    connection: sqlite3.Connection,
    case_id: str,
    left_lineage: ClaimLineage,
    right_lineage: ClaimLineage,
    known_lineages: dict[str, ClaimLineage],
) -> tuple[dict[str, Any], bool]:
    """快照化裁决可能连带修改的 claims 与相邻 open cases。"""

    endpoint_claims = (*left_lineage.claims, *right_lineage.claims)
    claims_by_id = {str(claim["id"]): claim for claim in endpoint_claims}
    if _is_exclusive_group(left_lineage.tip, right_lineage.tip):
        rows = connection.execute(
            "SELECT id,status,namespace_key,subject_entity_id,canonical_slot,value_json,qualifiers_json,"
            "valid_from,valid_to,recorded_from,recorded_to,observed_at,source_authority,confidence,"
            "assertion_kind,superseded_by_id FROM claims WHERE namespace_key=? AND conflict_key=? "
            "ORDER BY recorded_from,id",
            (left_lineage.tip["namespace_key"], left_lineage.tip["conflict_key"]),
        ).fetchall()
        claims_by_id.update({str(row["id"]): dict(row) for row in rows})
    close_claim_ids = set(claims_by_id)
    placeholders = ",".join("?" for _ in close_claim_ids)
    case_rows = connection.execute(
        "SELECT * FROM conflict_cases WHERE status IN ('pending','auto_resolved','manual_required') "
        "AND resolved_at IS NULL "
        f"AND (left_claim_id IN ({placeholders}) OR right_claim_id IN ({placeholders})) ORDER BY id",
        (*sorted(close_claim_ids), *sorted(close_claim_ids)),
    ).fetchall()
    close_case_rows = [
        row
        for row in case_rows
        if str(row["left_claim_id"]) in close_claim_ids and str(row["right_claim_id"]) in close_claim_ids
    ]
    close_endpoint_ids = sorted(
        {str(row[column]) for row in close_case_rows for column in ("left_claim_id", "right_claim_id")}
    )
    scope_lineages = {
        claim_id: known_lineages[claim_id] for claim_id in close_endpoint_ids if claim_id in known_lineages
    }
    missing_lineages = [claim_id for claim_id in close_endpoint_ids if claim_id not in scope_lineages]
    if missing_lineages:
        scope_lineages.update(resolve_claim_lineages(ClaimRepository(connection), missing_lineages))
    for lineage in scope_lineages.values():
        for claim in lineage.claims:
            claims_by_id.setdefault(str(claim["id"]), claim)
    endpoint_ids = {str(claim["id"]) for claim in endpoint_claims}
    survivor_contested = any(
        str(row["id"]) != case_id
        and (str(row["left_claim_id"]) in endpoint_ids or str(row["right_claim_id"]) in endpoint_ids)
        for row in case_rows
    )
    claim_ids = sorted(claims_by_id)
    return {
        "claims": [claims_by_id[claim_id] for claim_id in claim_ids],
        "cases": [dict(row) for row in case_rows],
        "lineages": {claim_id: scope_lineages[claim_id].snapshot() for claim_id in sorted(scope_lineages)},
    }, survivor_contested


def load_conflict_docket(connection: sqlite3.Connection, case_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT cases.*,state.dirty_at,state.dirty_reason,state.not_before,state.input_fingerprint,"
        "state.policy_version AS review_policy_version FROM conflict_cases AS cases "
        "LEFT JOIN conflict_review_state AS state ON state.case_id=cases.id WHERE cases.id=?",
        (case_id,),
    ).fetchone()
    if row is None:
        raise ConflictResolutionError(f"conflict case not found: {case_id}")
    case = dict(row)
    lineage_roots = [str(case["left_claim_id"]), str(case["right_claim_id"])]
    candidates, lineages = _load_candidates(connection, case_id, lineage_roots)
    left_lineage = lineages[str(case["left_claim_id"])]
    right_lineage = lineages[str(case["right_claim_id"])]
    left = left_lineage.tip
    right = right_lineage.tip
    claim_ids = [left_lineage.tip_id, right_lineage.tip_id]
    all_claim_ids = list(dict.fromkeys(str(claim["id"]) for lineage in lineages.values() for claim in lineage.claims))
    evidence, evidence_counts = _load_evidence(connection, all_claim_ids)
    group_native = bool(case.get("group_key") and candidates)
    if not candidates:
        candidates = [
            {
                "candidate_key": claim_id,
                "representative_claim_id": str(lineage.claims[0]["id"]),
                "representative_tip_id": claim_id,
                "support_count": 1,
                "evidence_count": evidence_counts[claim_id],
                "terminal": is_terminal_conflict_status(claim.get("status")),
            }
            for claim_id, claim, lineage in zip(
                claim_ids,
                (left, right),
                (left_lineage, right_lineage),
                strict=True,
            )
        ]
    case["group_native"] = group_native
    resolution_scope, survivor_contested = _load_resolution_scope(
        connection,
        case_id,
        left_lineage,
        right_lineage,
        lineages,
    )
    context = {
        "left_tip_id": left_lineage.tip_id,
        "right_tip_id": right_lineage.tip_id,
        "survivor_contested": survivor_contested,
        "entity_type_mismatch": False,
        "coordinates_complete": _coordinates_complete(left, right),
        "nonexclusive_false_positive": bool(
            left.get("canonical_slot") != right.get("canonical_slot")
            or (
                left.get("valid_from")
                and left.get("valid_from") == right.get("valid_from")
                and not _coordinates_complete(left, right)
            )
        ),
    }
    return {
        "case": case,
        "claims": [left, right],
        "lineages": {
            "left": left_lineage.snapshot(),
            "right": right_lineage.snapshot(),
        },
        "candidates": candidates,
        "evidence": evidence,
        "context": context,
        "resolution_scope": resolution_scope,
    }


def prepare_group_case_decisions(
    connection: sqlite3.Connection,
    claim_ids: list[str],
    winner_id: str,
) -> list[tuple[dict[str, Any], str]]:
    """在 winner mutation 前冻结重叠 case 的 side/group_winner 语义。"""

    placeholders = ",".join("?" for _ in claim_ids)
    rows = connection.execute(
        "SELECT * FROM conflict_cases "
        f"WHERE left_claim_id IN ({placeholders}) AND right_claim_id IN ({placeholders}) "
        "AND status IN ('pending','auto_resolved','manual_required') ORDER BY id",
        (*claim_ids, *claim_ids),
    ).fetchall()
    repository = ClaimRepository(connection)
    endpoint_ids = [str(row[column]) for row in rows for column in ("left_claim_id", "right_claim_id")]
    lineages = resolve_claim_lineages(repository, endpoint_ids)
    prepared: list[tuple[dict[str, Any], str]] = []
    for row in rows:
        case = dict(row)
        left_tip_id = lineages[str(case["left_claim_id"])].tip_id
        right_tip_id = lineages[str(case["right_claim_id"])].tip_id
        decision = "keep_left" if left_tip_id == winner_id else "keep_right"
        if winner_id not in {left_tip_id, right_tip_id}:
            decision = "group_winner"
        prepared.append((case, decision))
    return prepared


def project_pair_resolution(
    connection: sqlite3.Connection,
    case_id: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """把内部 pair 结果投影成后续 REST 可直接使用的稳定形状。"""

    current = connection.execute(
        "SELECT generation,revision FROM conflict_cases WHERE id=?",
        (case_id,),
    ).fetchone()
    return {
        "case_id": case_id,
        "generation": int(current["generation"]),
        "revision": int(current["revision"]),
        "status": result["status"],
        "decision": result["decision"],
        "winner_id": result["winner_id"],
        "resolved_at": result["resolved_at"],
        "closed_case_ids": result["closed_case_ids"],
    }


def assert_expected_conflict_fingerprint(
    connection: sqlite3.Connection,
    case_id: str,
    expected_fingerprint: str | None,
) -> None:
    if expected_fingerprint is None:
        return
    current = conflict_docket_fingerprint(load_conflict_docket(connection, case_id))
    if expected_fingerprint != current:
        raise StaleConflictDecision(f"stale conflict fingerprint: expected {expected_fingerprint}, current {current}")


def assert_terminal_rationale_immutable(case: dict[str, Any], rationale: str | None) -> None:
    if rationale is not None and rationale != case.get("rationale"):
        raise ConflictResolutionError(f"terminal conflict rationale is immutable: {case['id']}")


def _fingerprint_claim(claim: dict[str, Any]) -> dict[str, Any]:
    fingerprint = {key: claim.get(key) for key in _CLAIM_FINGERPRINT_FIELDS}
    for key, encoded_key in (("value", "value_json"), ("qualifiers", "qualifiers_json")):
        if key in claim:
            continue
        encoded = claim.get(encoded_key)
        try:
            fingerprint[key] = json.loads(str(encoded)) if encoded is not None else None
        except json.JSONDecodeError as error:
            raise ConflictResolutionError(f"invalid {encoded_key} for conflict claim: {claim.get('id')}") from error
    return fingerprint


def _fingerprint_lineage(lineage: dict[str, Any]) -> dict[str, Any]:
    return {
        "start_id": lineage["start_id"],
        "tip_id": lineage["tip_id"],
        "claims": [_fingerprint_claim(claim) for claim in lineage["claims"]],
        "edges": list(lineage["edges"]),
    }


def _fingerprint_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    member_lineages = candidate.get("member_lineages") or {}
    return {
        "candidate_key": candidate.get("candidate_key"),
        "canonical_value_json": candidate.get("canonical_value_json"),
        "representative_claim_id": candidate.get("representative_claim_id"),
        "representative_tip_id": candidate.get("representative_tip_id"),
        "support_count": candidate.get("support_count"),
        "evidence_count": candidate.get("evidence_count"),
        "first_seen_at": candidate.get("first_seen_at"),
        "last_seen_at": candidate.get("last_seen_at"),
        "claim_ids": sorted(str(item) for item in candidate.get("claim_ids") or ()),
        "claim_statuses": dict(sorted((candidate.get("claim_statuses") or {}).items())),
        "member_lineages": {
            member_id: _fingerprint_lineage(lineage) for member_id, lineage in sorted(member_lineages.items())
        },
        "terminal": bool(candidate.get("terminal")),
    }


def conflict_docket_fingerprint(docket: dict[str, Any]) -> str:
    lineages = docket["lineages"]
    resolution_scope = docket.get("resolution_scope") or {"claims": (), "cases": (), "lineages": {}}
    payload = {
        "version": "v2",
        "case": {key: docket["case"].get(key) for key in _CASE_FINGERPRINT_FIELDS},
        "tips": {
            side: _fingerprint_claim(claim) for side, claim in zip(("left", "right"), docket["claims"], strict=True)
        },
        "lineages": {side: _fingerprint_lineage(lineages[side]) for side in ("left", "right")},
        "candidates": sorted(
            (_fingerprint_candidate(candidate) for candidate in docket["candidates"]),
            key=lambda candidate: str(candidate["candidate_key"]),
        ),
        "evidence_ids": sorted({str(item.get("id")) for item in docket["evidence"]}),
        "resolution_scope": {
            "claims": sorted(
                (_fingerprint_claim(claim) for claim in resolution_scope["claims"]),
                key=lambda claim: str(claim["id"]),
            ),
            "cases": sorted(
                ({key: case.get(key) for key in _CASE_FINGERPRINT_FIELDS} for case in resolution_scope["cases"]),
                key=lambda case: str(case["id"]),
            ),
            "lineages": {
                claim_id: _fingerprint_lineage(lineage)
                for claim_id, lineage in sorted(resolution_scope["lineages"].items())
            },
        },
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
