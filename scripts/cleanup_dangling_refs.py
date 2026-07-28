"""清理记忆库中的悬空引用并恢复误标记的运行状态。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from hl_mem.storage.database import default_database_path


@dataclass(frozen=True)
class CleanupResult:
    """记录本次清理的统计结果与 observation 审计列表。"""

    dangling_evidence_links_deleted: int
    affected_claims: int
    affected_observations: int
    dangling_feedback_deleted: int
    observations_restored: tuple[str, ...]
    episodes_cancelled: int
    cancelled_episode_ids: tuple[str, ...]


def _now_iso() -> str:
    """返回 UTC ISO 8601 时间。"""
    return datetime.now(timezone.utc).isoformat()


def cleanup(connection: sqlite3.Connection) -> CleanupResult:
    """在单一事务中执行幂等清理并返回审计统计。"""
    connection.execute("BEGIN IMMEDIATE")
    try:
        dangling_links = connection.execute("""
            SELECT id, derived_type, derived_id
            FROM evidence_links AS link
            WHERE link.evidence_type = 'claim'
              AND NOT EXISTS (
                  SELECT 1 FROM claims AS claim WHERE claim.id = link.evidence_id
              )
            """).fetchall()
        affected_claims = len(
            {
                row["derived_id"]
                for row in dangling_links
                if row["derived_type"] == "claim"
            }
        )
        affected_observations = len(
            {
                row["derived_id"]
                for row in dangling_links
                if row["derived_type"] == "observation"
            }
        )

        # 清理前确定恢复集合，避免仅有悬空来源的 observation 在删除链接后漏选。
        observation_rows = connection.execute("""
            SELECT derivation.id
            FROM derivations AS derivation
            WHERE derivation.kind = 'observation'
              AND derivation.status = 'stale'
              AND EXISTS (
                  SELECT 1
                  FROM evidence_links AS link
                  WHERE link.derived_type = 'observation'
                    AND link.derived_id = derivation.id
                    AND link.evidence_type = 'claim'
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM evidence_links AS link
                  JOIN claims AS claim ON claim.id = link.evidence_id
                  WHERE link.derived_type = 'observation'
                    AND link.derived_id = derivation.id
                    AND link.evidence_type = 'claim'
                    AND claim.status <> 'active'
              )
            ORDER BY derivation.id
            """).fetchall()
        observation_ids = tuple(row["id"] for row in observation_rows)

        dangling_evidence_links_deleted = connection.execute("""
            DELETE FROM evidence_links
            WHERE evidence_type = 'claim'
              AND NOT EXISTS (
                  SELECT 1 FROM claims AS claim WHERE claim.id = evidence_links.evidence_id
              )
            """).rowcount
        dangling_feedback_deleted = connection.execute("""
            DELETE FROM retrieval_feedback
            WHERE memory_type = 'claim'
              AND NOT EXISTS (
                  SELECT 1 FROM claims AS claim WHERE claim.id = retrieval_feedback.memory_id
              )
            """).rowcount

        if observation_ids:
            placeholders = ",".join("?" for _ in observation_ids)
            connection.execute(
                f"UPDATE derivations SET status = 'active' WHERE id IN ({placeholders}) AND status = 'stale'",
                observation_ids,
            )

        episode_rows = connection.execute("""
            SELECT id
            FROM episodes
            WHERE status = 'running'
              AND goal IN ('test episode creation', 'connectivity test')
            ORDER BY id
            """).fetchall()
        episode_ids = tuple(row["id"] for row in episode_rows)
        episodes_cancelled = connection.execute(
            """
            UPDATE episodes
            SET status = 'cancelled', ended_at = ?
            WHERE status = 'running'
              AND goal IN ('test episode creation', 'connectivity test')
            """,
            (_now_iso(),),
        ).rowcount

        connection.commit()
        return CleanupResult(
            dangling_evidence_links_deleted=dangling_evidence_links_deleted,
            affected_claims=affected_claims,
            affected_observations=affected_observations,
            dangling_feedback_deleted=dangling_feedback_deleted,
            observations_restored=observation_ids,
            episodes_cancelled=episodes_cancelled,
            cancelled_episode_ids=episode_ids,
        )
    except Exception:
        connection.rollback()
        raise


def main(argv: Sequence[str] | None = None) -> int:
    """打开配置的数据库，执行清理并输出 JSON 审计结果。"""
    if argv:
        raise ValueError("cleanup_dangling_refs.py 不接受命令行参数")
    database_path = Path(default_database_path())
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    try:
        result = cleanup(connection)
    finally:
        connection.close()
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
