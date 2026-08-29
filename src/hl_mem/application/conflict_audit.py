"""冲突人工裁决的 governance action 审计写入。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from hl_mem.domain.governance import DecisionEnvelope, snapshot_fingerprint
from hl_mem.storage.governance import GovernanceActionRepository

CONFLICT_HUMAN_POLICY_VERSION = "conflict-human-resolution-v1"
MAX_RETRACTED_CLAIM_IDS = 64


class ConflictAuditWriter:
    """在调用方事务内记录人工冲突裁决。"""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        retracted_claim_ids: list[str] | None = None,
    ) -> None:
        self.connection = connection
        self.retracted_claim_ids = None if retracted_claim_ids is None else list(retracted_claim_ids)

    def record_human_action(
        self,
        *,
        case_id: str,
        decision: str,
        candidate_key: str | None,
        rationale: str | None,
        resolver: str,
        before_revision: int,
        after_revision: int,
        before_status: str,
        after_status: str,
        timestamp: str,
    ) -> None:
        action: dict[str, Any] = {
            "case_id": case_id,
            "decision": decision,
            "candidate_key": candidate_key,
            "rationale": rationale,
            "resolver": resolver,
        }
        if self.retracted_claim_ids is not None:
            sorted_ids = sorted(set(self.retracted_claim_ids))
            serialized_ids = json.dumps(sorted_ids, ensure_ascii=False, separators=(",", ":"))
            action.update(
                {
                    "retracted_claim_count": len(sorted_ids),
                    "retracted_claim_ids": sorted_ids[:MAX_RETRACTED_CLAIM_IDS],
                    "retracted_claim_ids_sha256": hashlib.sha256(serialized_ids.encode("utf-8")).hexdigest(),
                    "retracted_claim_ids_truncated": len(sorted_ids) > MAX_RETRACTED_CLAIM_IDS,
                }
            )
        envelope = DecisionEnvelope(
            domain="conflict",
            subject_ref=case_id,
            input_fingerprint=snapshot_fingerprint(
                {
                    **action,
                    "case_status": before_status,
                    "revision": before_revision,
                }
            ),
            policy_version=CONFLICT_HUMAN_POLICY_VERSION,
            tier="human",
            decision=decision,
            confidence=None,
            resolution_rule=rationale or "manual_resolution",
            resolver_model=resolver,
        )
        GovernanceActionRepository(self.connection).record(
            envelope,
            before={
                **action,
                "case_status": before_status,
                "revision": before_revision,
            },
            after={
                **action,
                "case_status": after_status,
                "revision": after_revision,
            },
            status="applied",
            created_at=timestamp,
            applied_at=timestamp,
        )
