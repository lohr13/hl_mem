"""检索反馈 usefulness 聚合存储。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Iterable, cast

from hl_mem.domain.feedback import BayesianUsefulnessPolicy
from hl_mem.protocols import MemoryType, UsefulnessPolicyProtocol, UsefulnessSnapshot

_TABLES = {"claim": "claims", "observation": "derivations", "policy": "policies"}


class UsefulnessRepository:
    """在同一事务内维护三类记忆的反馈聚合。"""

    def __init__(self, connection: sqlite3.Connection, policy: UsefulnessPolicyProtocol | None = None) -> None:
        self.connection = connection
        self.policy = policy or BayesianUsefulnessPolicy()

    def _validate(self, memory_type: str, memory_id: str) -> MemoryType:
        if memory_type not in _TABLES:
            raise ValueError(f"invalid memory type: {memory_type}")
        table = _TABLES[memory_type]
        if self.connection.execute(f"SELECT 1 FROM {table} WHERE id=?", (memory_id,)).fetchone() is None:
            raise ValueError(f"{memory_type} not found: {memory_id}")
        return cast(MemoryType, memory_type)

    def upsert(
        self,
        memory_type: MemoryType,
        memory_id: str,
        *,
        helpful_delta: int = 0,
        unhelpful_delta: int = 0,
        success_delta: float = 0.0,
        outcome_delta: int = 0,
        commit: bool = True,
    ) -> UsefulnessSnapshot:
        """应用增量并重算分数；负增量用于改票回滚旧聚合。"""
        kind = self._validate(memory_type, memory_id)
        existing = self.get(kind, memory_id)
        helpful = (existing.helpful_count if existing else 0) + helpful_delta
        unhelpful = (existing.unhelpful_count if existing else 0) + unhelpful_delta
        success = (existing.success_sum if existing else 0.0) + success_delta
        outcomes = (existing.outcome_count if existing else 0) + outcome_delta
        score, bonus = self.policy.evaluate(
            helpful_count=helpful,
            unhelpful_count=unhelpful,
            success_sum=success,
            outcome_count=outcomes,
        )
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute(
            "INSERT INTO memory_usefulness(memory_type,memory_id,helpful_count,unhelpful_count,success_sum,"
            "outcome_count,usefulness_score,retention_bonus_days,last_positive_at,last_negative_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(memory_type,memory_id) DO UPDATE SET "
            "helpful_count=excluded.helpful_count,unhelpful_count=excluded.unhelpful_count,"
            "success_sum=excluded.success_sum,outcome_count=excluded.outcome_count,"
            "usefulness_score=excluded.usefulness_score,retention_bonus_days=excluded.retention_bonus_days,"
            "last_positive_at=COALESCE(excluded.last_positive_at,memory_usefulness.last_positive_at),"
            "last_negative_at=COALESCE(excluded.last_negative_at,memory_usefulness.last_negative_at),"
            "updated_at=excluded.updated_at",
            (
                kind,
                memory_id,
                helpful,
                unhelpful,
                success,
                outcomes,
                score,
                bonus,
                now if helpful_delta > 0 or success_delta > 0 else None,
                now if unhelpful_delta > 0 else None,
                now,
            ),
        )
        if commit:
            self.connection.commit()
        snapshot = self.get(kind, memory_id)
        assert snapshot is not None
        return snapshot

    def get(self, memory_type: MemoryType, memory_id: str) -> UsefulnessSnapshot | None:
        """读取单条聚合快照。"""
        row = self.connection.execute(
            "SELECT * FROM memory_usefulness WHERE memory_type=? AND memory_id=?", (memory_type, memory_id)
        ).fetchone()
        return self._snapshot(row) if row else None

    def get_batch(self, memory_type: MemoryType, memory_ids: Iterable[str]) -> dict[str, UsefulnessSnapshot]:
        """批量读取聚合快照。"""
        ids = list(dict.fromkeys(memory_ids))
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self.connection.execute(
            f"SELECT * FROM memory_usefulness WHERE memory_type=? AND memory_id IN ({placeholders})",
            (memory_type, *ids),
        ).fetchall()
        return {row["memory_id"]: self._snapshot(row) for row in rows}

    def rebuild_all(self, *, commit: bool = True) -> int:
        """以 retrieval_feedback 为唯一事实源幂等重建全部聚合。"""
        self.connection.execute("DELETE FROM memory_usefulness")
        rows = self.connection.execute(
            "SELECT memory_type,memory_id,SUM(helpful=1),SUM(helpful=0),"
            "COALESCE(SUM(task_outcome),0),COUNT(task_outcome) FROM retrieval_feedback "
            "WHERE memory_type IN ('claim','observation','policy') AND (helpful IS NOT NULL OR task_outcome IS NOT NULL) "
            "GROUP BY memory_type,memory_id"
        ).fetchall()
        count = 0
        for row in rows:
            try:
                self.upsert(
                    row[0],
                    row[1],
                    helpful_delta=int(row[2] or 0),
                    unhelpful_delta=int(row[3] or 0),
                    success_delta=float(row[4] or 0.0),
                    outcome_delta=int(row[5] or 0),
                    commit=False,
                )
                count += 1
            except ValueError:
                continue
        if commit:
            self.connection.commit()
        return count

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> UsefulnessSnapshot:
        return UsefulnessSnapshot(
            memory_type=row["memory_type"],
            memory_id=row["memory_id"],
            helpful_count=row["helpful_count"],
            unhelpful_count=row["unhelpful_count"],
            success_sum=row["success_sum"],
            outcome_count=row["outcome_count"],
            usefulness_score=row["usefulness_score"],
            retention_bonus_days=row["retention_bonus_days"],
            updated_at=row["updated_at"],
        )
