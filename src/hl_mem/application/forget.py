"""记忆撤回应用服务。原子化撤回 Claim，清除向量，传播 stale 标记。"""

from __future__ import annotations

from typing import Any

from hl_mem.lifecycle import assert_transition
from hl_mem.recall.recall_pipeline import stale_observations
from hl_mem.storage.repository import ClaimRepository


class ForgetService:
    """记忆撤回应用服务。"""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def forget(self, memory_id: str) -> dict[str, Any]:
        """撤回 claim、清除向量并原子传播 observation 失效标记。"""
        repository = ClaimRepository(self.connection)
        claim = repository.get_claim(memory_id)
        if not claim:
            raise ValueError(f"memory not found: {memory_id}")
        assert_transition(claim["status"], "retracted")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self.connection.execute(
                "UPDATE claims SET status='retracted',embedding_dense=NULL,embedding_sparse=NULL WHERE id=?",
                (memory_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"memory not found: {memory_id}")
            stale_observations(self.connection, memory_id, commit=False)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return {"id": memory_id, "forgotten": True}
