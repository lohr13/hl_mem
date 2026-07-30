"""从检索反馈事实源重建 usefulness 聚合。"""

from __future__ import annotations

import sqlite3

from hl_mem.domain.feedback import BayesianUsefulnessPolicy
from hl_mem.settings import Settings
from hl_mem.storage.usefulness import UsefulnessRepository


def rebuild_usefulness(connection: sqlite3.Connection, settings: Settings) -> dict[str, int]:
    """幂等全量重建 memory_usefulness。"""
    policy = BayesianUsefulnessPolicy(
        settings.feedback_bonus_every,
        settings.feedback_bonus_days,
        settings.feedback_bonus_cap_days,
    )
    connection.execute("BEGIN IMMEDIATE")
    try:
        rebuilt = UsefulnessRepository(connection, policy).rebuild_all(commit=False)
        connection.commit()
        return {"rebuilt": rebuilt}
    except Exception:
        connection.rollback()
        raise
