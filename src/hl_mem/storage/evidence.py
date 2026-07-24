"""证据链接和派生记忆仓储。"""

from __future__ import annotations

import sqlite3
from typing import Any

from hl_mem.storage._shared import insert_row, row_to_dict


class EvidenceRepository:
    """提供证据链接的写入和批量查询。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add_link(self, link: dict[str, Any], commit: bool = True) -> bool:
        """写入证据链接。"""
        return insert_row(self.connection, "evidence_links", link, commit)

    def get_links_for_derived(self, derived_type: str, derived_id: str) -> list[dict[str, Any]]:
        """返回派生对象的证据链接。"""
        rows = self.connection.execute(
            "SELECT * FROM evidence_links WHERE derived_type=? AND derived_id=?", (derived_type, derived_id)
        ).fetchall()
        return [dict(row) for row in rows]

    def batch_get_links_for_derived(self, derived_type: str, derived_ids: list[str]) -> dict[str, list[dict[str, str]]]:
        """批量获取派生对象的证据链接，并按 derived_id 分组。"""
        unique_ids = list(dict.fromkeys(derived_ids))
        if not unique_ids:
            return {}
        result: dict[str, list[dict[str, str]]] = {derived_id: [] for derived_id in unique_ids}
        for start in range(0, len(unique_ids), 500):
            chunk = unique_ids[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = self.connection.execute(
                f"SELECT * FROM evidence_links WHERE derived_type=? AND derived_id IN ({placeholders})",
                (derived_type, *chunk),
            ).fetchall()
            for row in rows:
                result[row["derived_id"]].append({"type": row["evidence_type"], "id": row["evidence_id"]})
        return result

    def get_links_for_evidence(self, evidence_type: str, evidence_id: str) -> list[dict[str, Any]]:
        """返回直接引用指定证据的链接。"""
        rows = self.connection.execute(
            "SELECT * FROM evidence_links WHERE evidence_type=? AND evidence_id=?", (evidence_type, evidence_id)
        ).fetchall()
        return [dict(row) for row in rows]


class DerivationRepository:
    """提供派生记忆的写入、状态更新和召回查询。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def insert_observation(self, observation: dict[str, Any]) -> bool:
        """写入 observation 派生记忆。"""
        return insert_row(self.connection, "derivations", {"kind": "observation", **observation})

    def get_observation(self, observation_id: str) -> dict[str, Any] | None:
        """按标识返回派生记忆。"""
        return row_to_dict(
            self.connection.execute("SELECT * FROM derivations WHERE id=?", (observation_id,)).fetchone()
        )

    def list_active_for_claims(self, claim_ids: list[str], limit: int = 10) -> list[dict[str, Any]]:
        """返回与给定声明相关的活跃派生记忆。"""
        if not claim_ids:
            return []
        placeholders = ",".join("?" for _ in claim_ids)
        rows = self.connection.execute(
            "SELECT d.id,d.kind,d.body,d.confidence,d.updated_at FROM derivations d "
            "JOIN evidence_links e ON e.derived_id=d.id AND e.derived_type=d.kind "
            f"WHERE d.status='active' AND e.evidence_type='claim' AND e.evidence_id IN ({placeholders}) "
            "GROUP BY d.id ORDER BY d.updated_at DESC LIMIT ?",
            (*claim_ids, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def update_status(self, observation_id: str, status: str, commit: bool = True) -> bool:
        """更新派生记忆状态。"""
        cursor = self.connection.execute("UPDATE derivations SET status=? WHERE id=?", (status, observation_id))
        if commit:
            self.connection.commit()
        return cursor.rowcount == 1
