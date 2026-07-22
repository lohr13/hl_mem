"""维护带证据链的 Observation、Mental Model 与 Session Summary。"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any


class DerivedMemoryMaintainer:
    """以幂等方式重建派生记忆并传播失效依赖。"""

    _KINDS = {"observation", "mental_model", "session_summary"}

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def rebuild(
        self,
        derivation_id: str,
        kind: str,
        body: str,
        evidence_ids: list[str],
        updated_at: str,
    ) -> dict[str, Any]:
        """使用有效且互不重复的 Claim 证据重建一条派生记忆。"""
        if kind not in self._KINDS:
            raise ValueError(f"unsupported derivation kind: {kind}")
        unique_ids = list(dict.fromkeys(evidence_ids))
        if not unique_ids:
            raise ValueError("active derivation requires evidence")
        placeholders = ",".join("?" for _ in unique_ids)
        rows = self.connection.execute(
            f"SELECT id,recorded_from FROM claims WHERE id IN ({placeholders}) AND status='active'",
            unique_ids,
        ).fetchall()
        if len(rows) != len(unique_ids):
            raise ValueError("all evidence claims must be active")
        watermark = max(row["recorded_from"] for row in rows)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                "INSERT INTO derivations(id,kind,body,status,source_watermark,proof_count,updated_at) "
                "VALUES (?,?,?,'active',?,?,?) ON CONFLICT(id) DO UPDATE SET kind=excluded.kind,"
                "body=excluded.body,status='active',source_watermark=excluded.source_watermark,"
                "proof_count=excluded.proof_count,updated_at=excluded.updated_at",
                (derivation_id, kind, body, watermark, len(unique_ids), updated_at),
            )
            self.connection.execute(
                "DELETE FROM evidence_links WHERE derived_type=? AND derived_id=? AND relation='supports'",
                (kind, derivation_id),
            )
            for claim_id in unique_ids:
                self.connection.execute(
                    "INSERT INTO evidence_links(id,derived_type,derived_id,evidence_type,evidence_id,relation,weight) "
                    "VALUES (?,?,?,?,?,'supports',1.0)",
                    (uuid.uuid4().hex, kind, derivation_id, "claim", claim_id),
                )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return self.get(derivation_id)

    def mark_stale_dependencies(self) -> int:
        """将依赖非活动 Claim 的派生记忆标记为 stale。"""
        cursor = self.connection.execute(
            "UPDATE derivations SET status='stale' WHERE status='active' AND EXISTS ("
            "SELECT 1 FROM evidence_links e JOIN claims c ON c.id=e.evidence_id "
            "WHERE e.derived_id=derivations.id AND e.evidence_type='claim' "
            "AND c.status NOT IN ('active'))"
        )
        self.connection.commit()
        return cursor.rowcount

    def get(self, derivation_id: str) -> dict[str, Any]:
        """返回指定派生记忆。"""
        row = self.connection.execute("SELECT * FROM derivations WHERE id=?", (derivation_id,)).fetchone()
        if not row:
            raise ValueError(f"derivation not found: {derivation_id}")
        return dict(row)
