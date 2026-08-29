"""供宿主 agent 轮询和裁决使用的冲突只读查询。"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Sequence
from typing import Any

from hl_mem.application.conflict_snapshot import (
    conflict_docket_fingerprint,
    load_conflict_docket,
)
from hl_mem.errors import ConflictResolutionError, NotFoundError, ValidationError

OPEN_CASE_STATUSES = ("pending", "auto_resolved", "manual_required")
MAX_DOSSIER_RESPONSE_BYTES = 1024 * 1024


class ConflictDossierTooLargeError(Exception):
    """冲突案卷超过固定 REST 响应上限。"""


def load_conflict_case(connection: sqlite3.Connection, case_id: str) -> dict[str, Any]:
    """加载 ResolutionService 支持的冲突状态。"""

    row = connection.execute("SELECT * FROM conflict_cases WHERE id=?", (case_id,)).fetchone()
    if row is None:
        raise ConflictResolutionError(f"conflict case not found: {case_id}")
    case = dict(row)
    if case["status"] not in {*OPEN_CASE_STATUSES, "resolved", "rejected"}:
        raise ConflictResolutionError(f"unsupported conflict case status: {case['status']}")
    return case


class ConflictQueryService:
    """组装冲突案卷和可分页的未闭合 case 列表。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def review(self, case_id: str) -> dict[str, Any]:
        """返回一个 revision 快照及其全部 canonical candidates。"""

        docket = load_conflict_docket(self.connection, case_id)
        case = docket["case"]
        candidates: list[dict[str, Any]] = []
        raw_candidates = docket["candidates"] if case.get("group_key") else []
        for raw_candidate in raw_candidates:
            try:
                canonical_value = json.loads(str(raw_candidate["canonical_value_json"]))
            except json.JSONDecodeError:
                canonical_value = raw_candidate["canonical_value_json"]
            candidates.append(
                {
                    "candidate_key": str(raw_candidate["candidate_key"]),
                    "canonical_value": canonical_value,
                    "representative_claim_id": str(raw_candidate["representative_claim_id"]),
                    "representative_tip_id": str(raw_candidate["representative_tip_id"]),
                    "support_count": int(raw_candidate["support_count"]),
                    "evidence_count": int(raw_candidate["evidence_count"]),
                    "first_seen_at": str(raw_candidate["first_seen_at"]),
                    "last_seen_at": str(raw_candidate["last_seen_at"]),
                    "claim_ids": list(raw_candidate["claim_ids"]),
                    "claim_statuses": dict(raw_candidate["claim_statuses"]),
                }
            )
        return {
            "case_id": str(case["id"]),
            "namespace": case.get("namespace_key"),
            "group_key": case.get("group_key"),
            "generation": int(case.get("generation") or 1),
            "revision": int(case.get("revision") or 0),
            "fingerprint_version": "v2",
            "fingerprint": conflict_docket_fingerprint(docket),
            "status": str(case["status"]),
            "overflow": bool(case.get("overflow")),
            "candidate_count": len(candidates),
            "candidates": candidates,
        }

    def dossier(self, case_id: str) -> dict[str, Any]:
        """返回 pair/group 共用的完整裁决案卷。"""

        try:
            snapshot = load_conflict_docket(self.connection, case_id)
        except ConflictResolutionError as error:
            if str(error) == f"conflict case not found: {case_id}":
                raise NotFoundError(str(error)) from error
            raise
        case = snapshot["case"]
        remaining_bytes = MAX_DOSSIER_RESPONSE_BYTES

        def consume_budget(value: Any, *, occurrences: int = 1) -> None:
            nonlocal remaining_bytes
            remaining_bytes -= self._serialized_size(value) * occurrences
            if remaining_bytes < 0:
                raise ConflictDossierTooLargeError(
                    f"conflict dossier exceeds {MAX_DOSSIER_RESPONSE_BYTES} bytes: {case_id}"
                )

        fixed_fields = {
            "case_id": str(case["id"]),
            "pair_key": str(case["pair_key"]),
            "status": str(case["status"]),
            "created_at": str(case["created_at"]),
            "revision": int(case.get("revision") or 0),
            "fingerprint_version": "v2",
            "fingerprint": conflict_docket_fingerprint(snapshot),
            "namespace_key": case.get("namespace_key"),
            "group_key": case.get("group_key"),
            "overflow": bool(case.get("overflow")),
            "left_tip_id": str(snapshot["context"]["left_tip_id"]),
            "right_tip_id": str(snapshot["context"]["right_tip_id"]),
        }
        consume_budget(fixed_fields)
        left_claim_id = str(snapshot["context"]["left_tip_id"])
        right_claim_id = str(snapshot["context"]["right_tip_id"])
        left_lineage_ids = [str(claim["id"]) for claim in snapshot["lineages"]["left"]["claims"]]
        right_lineage_ids = [str(claim["id"]) for claim in snapshot["lineages"]["right"]["claims"]]
        occurrences: Counter[str] = Counter((*left_lineage_ids, *right_lineage_ids))
        occurrences.update((left_claim_id, right_claim_id))
        claims_by_id: dict[str, dict[str, Any]] = {}
        for lineage in (snapshot["lineages"]["left"], snapshot["lineages"]["right"]):
            claims_by_id.update({str(claim["id"]): claim for claim in lineage["claims"]})
        raw_candidates = snapshot["candidates"] if case.get("group_key") else []
        for candidate in raw_candidates:
            for member_id, lineage in (candidate.get("member_lineages") or {}).items():
                occurrences[str(member_id)] += 1
                for claim in lineage["claims"]:
                    claim_id = str(claim["id"])
                    occurrences[claim_id] += 1
                    claims_by_id[claim_id] = claim

        evidence_by_claim: dict[str, list[dict[str, Any]]] = {claim_id: [] for claim_id in claims_by_id}
        for evidence in snapshot["evidence"]:
            evidence_by_claim[str(evidence["derived_id"])].append(
                {key: value for key, value in evidence.items() if key != "derived_id"}
            )
        claim_cache: dict[str, dict[str, Any]] = {}
        for claim_id, claim in claims_by_id.items():
            detail = {
                "id": claim_id,
                "canonical_slot": claim.get("canonical_slot"),
                "value": claim.get("value"),
                "qualifiers": dict(claim.get("qualifiers") or {}),
                "subject_entity_id": claim.get("subject_entity_id"),
                "assertion_kind": str(claim.get("assertion_kind") or "unknown"),
                "confidence": claim.get("confidence"),
                "source_authority": claim.get("source_authority"),
                "valid_from": claim.get("valid_from"),
                "valid_to": claim.get("valid_to"),
                "recorded_from": claim.get("recorded_from"),
                "recorded_to": claim.get("recorded_to"),
                "superseded_by_id": claim.get("superseded_by_id"),
                "observed_at": claim.get("observed_at"),
                "status": str(claim["status"]),
                "evidence_links": evidence_by_claim[claim_id],
            }
            consume_budget(detail, occurrences=occurrences[claim_id])
            claim_cache[claim_id] = detail

        candidates: list[dict[str, Any]] = []
        for candidate in raw_candidates:
            candidate_metadata = {
                "candidate_key": str(candidate["candidate_key"]),
                "representative_claim_id": str(candidate["representative_claim_id"]),
                "representative_tip_id": str(candidate["representative_tip_id"]),
                "support_count": int(candidate["support_count"]),
                "canonical_value_json": str(candidate["canonical_value_json"]),
            }
            consume_budget(candidate_metadata)
            member_ids = [str(member_id) for member_id in candidate["claim_ids"]]
            candidates.append(
                {
                    **candidate_metadata,
                    "member_claims": [claim_cache[member_id] for member_id in member_ids],
                    "member_lineages": {
                        member_id: [claim_cache[str(claim["id"])] for claim in lineage["claims"]]
                        for member_id, lineage in candidate["member_lineages"].items()
                    },
                }
            )

        dossier = {
            **fixed_fields,
            "left_claim": claim_cache[left_claim_id],
            "right_claim": claim_cache[right_claim_id],
            "left_lineage": [claim_cache[claim_id] for claim_id in left_lineage_ids],
            "right_lineage": [claim_cache[claim_id] for claim_id in right_lineage_ids],
            "candidates": candidates,
        }
        rendered_size = self._serialized_size(dossier)
        if rendered_size > MAX_DOSSIER_RESPONSE_BYTES:
            raise ConflictDossierTooLargeError(
                f"conflict dossier exceeds {MAX_DOSSIER_RESPONSE_BYTES} bytes: {case_id}"
            )
        return dossier

    def list_open_cases(
        self,
        *,
        statuses: Sequence[str] | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """按状态过滤并分页返回尚未落终态的冲突。"""

        selected = tuple(dict.fromkeys(status.strip() for status in (statuses or OPEN_CASE_STATUSES)))
        invalid = [status for status in selected if status not in OPEN_CASE_STATUSES]
        if not selected or invalid:
            detail = ",".join(invalid) if invalid else "empty"
            raise ValidationError(f"unsupported open conflict status: {detail}")
        placeholders = ",".join("?" for _ in selected)
        where = f"cases.status IN ({placeholders}) AND cases.resolved_at IS NULL"
        total = int(
            self.connection.execute(
                f"SELECT count(*) FROM conflict_cases AS cases WHERE {where}",
                selected,
            ).fetchone()[0]
        )
        rows = self.connection.execute(
            "SELECT cases.id AS case_id,cases.status,cases.created_at,"
            "COALESCE(cases.namespace_key,left_claim.namespace_key) AS namespace,"
            "cases.group_key,COALESCE(left_claim.canonical_slot,right_claim.canonical_slot) AS slot,"
            "cases.revision FROM conflict_cases AS cases "
            "JOIN claims AS left_claim ON left_claim.id=cases.left_claim_id "
            "JOIN claims AS right_claim ON right_claim.id=cases.right_claim_id "
            f"WHERE {where} ORDER BY cases.created_at,cases.id LIMIT ? OFFSET ?",
            (*selected, limit, offset),
        ).fetchall()
        return {
            "cases": [
                {
                    "case_id": str(row["case_id"]),
                    "status": str(row["status"]),
                    "created_at": str(row["created_at"]),
                    "namespace": str(row["namespace"]),
                    "group_key": row["group_key"],
                    "slot": row["slot"],
                    "revision": int(row["revision"]),
                }
                for row in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @staticmethod
    def _serialized_size(value: Any) -> int:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8"))
