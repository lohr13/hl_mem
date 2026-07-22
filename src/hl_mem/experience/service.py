"""管理 Episode、Trace 和内嵌 Procedure 的 Policy。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any


def _id() -> str:
    return uuid.uuid4().hex


class ExperienceService:
    """以事务方式维护经验证据和策略生命周期。"""

    def __init__(self, connection: sqlite3.Connection, min_support: int = 2, retire_after_failures: int = 3) -> None:
        if min_support < 2:
            raise ValueError("min_support must be at least 2")
        if retire_after_failures < 1:
            raise ValueError("retire_after_failures must be positive")
        self.connection = connection
        self.min_support = min_support
        self.retire_after_failures = retire_after_failures

    def record_episode(self, episode_id: str, goal: str, status: str, reward: float, occurred_at: str) -> str:
        """记录一次独立 Episode 并返回其 ID。"""
        self.connection.execute(
            "INSERT OR IGNORE INTO episodes(id,goal,status,started_at,ended_at,reward,outcome_summary) "
            "VALUES (?,?,?,?,?,?,?)",
            (episode_id, goal, status, occurred_at, occurred_at, reward, status),
        )
        self.connection.commit()
        return episode_id

    def add_trace(
        self, episode_id: str, action: str, observation: str | None, error_signature: str | None, value: float
    ) -> str:
        """向 Episode 追加一个有序 Trace。"""
        sequence_no = self.connection.execute(
            "SELECT coalesce(max(sequence_no),0)+1 FROM traces WHERE episode_id=?", (episode_id,)
        ).fetchone()[0]
        trace_id = _id()
        self.connection.execute(
            "INSERT INTO traces(id,episode_id,sequence_no,action,observation,error_signature,value) VALUES (?,?,?,?,?,?,?)",
            (trace_id, episode_id, sequence_no, action, observation, error_signature, value),
        )
        self.connection.commit()
        return trace_id

    def induce_policy(
        self, trigger: str, procedure: dict[str, Any], episode_ids: list[str], created_at: str, namespace: str = "default"
    ) -> str:
        """从独立成功 Episode 归纳候选策略。"""
        unique_ids = list(dict.fromkeys(episode_ids))
        if not unique_ids:
            raise ValueError("at least one supporting episode is required")
        placeholders = ",".join("?" for _ in unique_ids)
        rows = self.connection.execute(
            f"SELECT id FROM episodes WHERE id IN ({placeholders}) AND status='success' AND reward>0", unique_ids
        ).fetchall()
        valid_ids = [row[0] for row in rows]
        if len(valid_ids) != len(unique_ids):
            raise ValueError("all supporting episodes must be independent successes")
        policy_id = _id()
        status = "active" if len(valid_ids) >= self.min_support else "candidate"
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                "INSERT INTO policies(id,namespace_key,trigger,procedure,support,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (policy_id, namespace, trigger, json.dumps(procedure, ensure_ascii=False), len(valid_ids), status, created_at, created_at),
            )
            for episode_id in valid_ids:
                self._link_episode(policy_id, episode_id)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return policy_id

    def add_support(self, policy_id: str, episode_id: str) -> None:
        """为策略增加一条未重复的成功 Episode 证据。"""
        episode = self.connection.execute(
            "SELECT status,reward FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        if not episode or episode["status"] != "success" or episode["reward"] <= 0:
            raise ValueError("supporting episode must be successful")
        before = self.connection.total_changes
        self._link_episode(policy_id, episode_id)
        if self.connection.total_changes > before:
            self.connection.execute(
                "UPDATE policies SET support=support+1,status=CASE WHEN support+1>=? THEN 'active' ELSE status END WHERE id=?",
                (self.min_support, policy_id),
            )
        self.connection.commit()

    def record_policy_outcome(self, policy_id: str, succeeded: bool, occurred_at: str) -> None:
        """回写 Procedure 使用结果，并按可靠度激活或退休。"""
        success_delta, failure_delta = (1, 0) if succeeded else (0, 1)
        consecutive = 0 if succeeded else 1
        self.connection.execute(
            "UPDATE policies SET success_count=success_count+?,failure_count=failure_count+?,"
            "consecutive_failures=CASE WHEN ?=1 THEN 0 ELSE consecutive_failures+1 END,updated_at=? WHERE id=?",
            (success_delta, failure_delta, success_delta, occurred_at, policy_id),
        )
        self.connection.execute(
            "UPDATE policies SET reliability=CAST(success_count AS REAL)/max(1,success_count+failure_count),"
            "procedure_status=CASE WHEN consecutive_failures>=? THEN 'retired' WHEN success_count>0 THEN 'active' "
            "ELSE procedure_status END,status=CASE WHEN consecutive_failures>=? THEN 'retired' ELSE status END WHERE id=?",
            (self.retire_after_failures, self.retire_after_failures, policy_id),
        )
        self.connection.commit()

    def get_policy(self, policy_id: str) -> dict[str, Any]:
        """返回策略记录。"""
        row = self.connection.execute("SELECT * FROM policies WHERE id=?", (policy_id,)).fetchone()
        if not row:
            raise ValueError(f"policy not found: {policy_id}")
        return dict(row)

    def _link_episode(self, policy_id: str, episode_id: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO evidence_links(id,derived_type,derived_id,evidence_type,evidence_id,relation,weight) "
            "SELECT ?, 'policy', ?, 'episode', ?, 'supports', 1.0 WHERE NOT EXISTS "
            "(SELECT 1 FROM evidence_links WHERE derived_type='policy' AND derived_id=? AND evidence_type='episode' AND evidence_id=?)",
            (_id(), policy_id, episode_id, policy_id, episode_id),
        )
