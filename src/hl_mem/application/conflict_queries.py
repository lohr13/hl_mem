"""供宿主 agent 轮询和裁决使用的冲突只读查询。"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Sequence
from typing import Any

from hl_mem.application.conflicts import OPEN_CASE_STATUSES
from hl_mem.errors import NotFoundError, ValidationError
from hl_mem.storage.claims import ClaimRepository

MAX_EVIDENCE_PER_CLAIM = 5
MAX_EVENT_CONTENT_CHARS = 500
MAX_DOSSIER_RESPONSE_BYTES = 1024 * 1024
CLAIM_BATCH_SIZE = 100


class ConflictDossierTooLargeError(Exception):
    """冲突案卷超过固定 REST 响应上限。"""


class ConflictQueryService:
    """组装冲突案卷和可分页的未闭合 case 列表。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.claims = ClaimRepository(connection)

    def dossier(self, case_id: str) -> dict[str, Any]:
        """返回 pair/group 共用的完整裁决案卷。"""

        row = self.connection.execute("SELECT * FROM conflict_cases WHERE id=?", (case_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"conflict case not found: {case_id}")
        case = dict(row)
        remaining_bytes = MAX_DOSSIER_RESPONSE_BYTES
        claim_cache: dict[str, dict[str, Any]] = {}

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
            "namespace_key": case.get("namespace_key"),
            "group_key": case.get("group_key"),
            "overflow": bool(case.get("overflow")),
        }
        consume_budget(fixed_fields)
        left_claim_id = str(case["left_claim_id"])
        right_claim_id = str(case["right_claim_id"])
        claim_ids = list(dict.fromkeys((left_claim_id, right_claim_id)))
        seen_claim_ids = set(claim_ids)
        occurrences: Counter[str] = Counter((left_claim_id, right_claim_id))
        candidate_builders: list[dict[str, Any]] = []
        current_candidate: dict[str, Any] | None = None
        current_candidate_key: str | None = None
        candidate_rows = self.connection.execute(
            "SELECT candidates.candidate_key,candidates.representative_claim_id,"
            "candidates.support_count,candidates.canonical_value_json,members.claim_id "
            "FROM conflict_case_candidates AS candidates "
            "LEFT JOIN conflict_candidate_members AS members "
            "ON members.case_id=candidates.case_id AND members.candidate_key=candidates.candidate_key "
            "WHERE candidates.case_id=? ORDER BY candidates.candidate_key,members.claim_id",
            (case_id,),
        )
        for candidate_row in candidate_rows:
            candidate_key = str(candidate_row["candidate_key"])
            if candidate_key != current_candidate_key:
                current_candidate_key = candidate_key
                current_candidate = {
                    "candidate_key": candidate_key,
                    "representative_claim_id": str(candidate_row["representative_claim_id"]),
                    "support_count": int(candidate_row["support_count"]),
                    "canonical_value_json": str(candidate_row["canonical_value_json"]),
                    "member_claim_ids": [],
                }
                candidate_builders.append(current_candidate)
                consume_budget({key: value for key, value in current_candidate.items() if key != "member_claim_ids"})
            member_claim_id = candidate_row["claim_id"]
            if member_claim_id is None or current_candidate is None:
                continue
            member_id = str(member_claim_id)
            consume_budget(member_id)
            current_candidate["member_claim_ids"].append(member_id)
            occurrences[member_id] += 1
            if member_id not in seen_claim_ids:
                seen_claim_ids.add(member_id)
                claim_ids.append(member_id)

        for start in range(0, len(claim_ids), CLAIM_BATCH_SIZE):
            batch = claim_ids[start : start + CLAIM_BATCH_SIZE]
            placeholders = ",".join("?" for _ in batch)
            raw_value_bytes = int(
                self.connection.execute(
                    "SELECT COALESCE(sum(length(CAST(value_json AS BLOB))),0) FROM claims "
                    f"WHERE id IN ({placeholders})",
                    batch,
                ).fetchone()[0]
            )
            if raw_value_bytes > remaining_bytes:
                raise ConflictDossierTooLargeError(
                    f"conflict dossier exceeds {MAX_DOSSIER_RESPONSE_BYTES} bytes: {case_id}"
                )
            claims = self.claims.batch_get_claims(batch)
            evidence_by_claim = self._batch_evidence_links(batch)
            for claim_id in batch:
                claim = claims.get(claim_id)
                if claim is None:
                    raise NotFoundError(f"claim not found: {claim_id}")
                detail = {
                    "id": str(claim["id"]),
                    "canonical_slot": claim.get("canonical_slot"),
                    "value": claim.get("value"),
                    "subject_entity_id": claim.get("subject_entity_id"),
                    "assertion_kind": str(claim.get("assertion_kind") or "unknown"),
                    "confidence": claim.get("confidence"),
                    "source_authority": claim.get("source_authority"),
                    "valid_from": claim.get("valid_from"),
                    "valid_to": claim.get("valid_to"),
                    "observed_at": claim.get("observed_at"),
                    "status": str(claim["status"]),
                    "evidence_links": evidence_by_claim[claim_id],
                }
                consume_budget(detail, occurrences=occurrences[claim_id])
                claim_cache[claim_id] = detail

        candidates = [
            {
                "candidate_key": candidate["candidate_key"],
                "representative_claim_id": candidate["representative_claim_id"],
                "support_count": candidate["support_count"],
                "canonical_value_json": candidate["canonical_value_json"],
                "member_claims": [claim_cache[claim_id] for claim_id in candidate["member_claim_ids"]],
            }
            for candidate in candidate_builders
        ]

        dossier = {
            **fixed_fields,
            "left_claim": claim_cache[left_claim_id],
            "right_claim": claim_cache[right_claim_id],
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

    def _batch_evidence_links(self, claim_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        placeholders = ",".join("?" for _ in claim_ids)
        rows = self.connection.execute(
            "WITH ranked AS (SELECT links.derived_id,links.id,links.evidence_type,"
            "links.evidence_id,links.relation,links.weight,events.event_type,events.occurred_at,"
            "events.content_json,ROW_NUMBER() OVER (PARTITION BY links.derived_id ORDER BY links.id) "
            "AS evidence_rank FROM evidence_links AS links LEFT JOIN events "
            "ON links.evidence_type='event' AND events.id=links.evidence_id "
            f"WHERE links.derived_type='claim' AND links.derived_id IN ({placeholders})) "
            "SELECT * FROM ranked WHERE evidence_rank<=? ORDER BY derived_id,id",
            (*claim_ids, MAX_EVIDENCE_PER_CLAIM),
        ).fetchall()
        result: dict[str, list[dict[str, Any]]] = {claim_id: [] for claim_id in claim_ids}
        for row in rows:
            result[str(row["derived_id"])].append(
                {
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
        return result

    @staticmethod
    def _serialized_size(value: Any) -> int:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8"))
