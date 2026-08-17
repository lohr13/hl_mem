"""Experience 应用服务。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Callable

from hl_mem.settings import Settings
from hl_mem.storage.deferred_tasks import DeferredTaskRepository
from hl_mem.storage.experience import (
    ExperienceRepository,
    InvalidStateTransitionError,
    backprop_episode_reward,
)


class ExperienceService:
    """Experience 应用入口，协调 exposure 物化与 delivery receipt。"""

    def __init__(
        self,
        connection: sqlite3.Connection,
        min_support: int = 2,
        retire_after_failures: int = 3,
        settings: Settings | None = None,
        pending_exposure_check: Callable[[list[str]], bool] | None = None,
    ) -> None:
        self.connection = connection
        self.settings = settings or getattr(connection, "hl_mem_settings", None) or Settings()
        self.pending_exposure_check = pending_exposure_check
        self.repository = ExperienceRepository(
            connection,
            min_support=min_support,
            retire_after_failures=retire_after_failures,
            settings=self.settings,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self.repository, name)

    def get_episode(self, episode_id: str) -> dict[str, Any]:
        return self.repository.get_episode(episode_id)

    def update_episode(
        self,
        episode_id: str,
        updated_at: str,
        status: str | None = None,
        reward: float | None = None,
        outcome_summary: str | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        return self.repository.update_episode(
            episode_id,
            updated_at,
            status,
            reward,
            outcome_summary,
            commit,
        )

    def record_exposure_batch(self, exposures: list[tuple[Any, ...]]) -> int:
        """原子持久化最终 packet items 对应的 exposure。"""
        return self.repository.record_exposure_batch(exposures)

    def mark_feedback_injected_batch(self, feedback_ids: list[str]) -> int:
        """原子标记已经交付到 Agent host/model 输入边界的 exposure。"""
        return self.repository.mark_feedback_injected_batch(feedback_ids)

    def submit_retrieval_feedback_eventually(
        self,
        feedback_id: str,
        helpful: bool,
        task_outcome: float | None,
        created_at: str,
    ) -> dict[str, bool]:
        """exposure 尚未可见时持久登记依赖反馈，避免 receipt 竞态。"""
        try:
            return self.repository.submit_retrieval_feedback(feedback_id, helpful, task_outcome, created_at)
        except ValueError as error:
            if not str(error).startswith("feedback exposure not found:"):
                raise
            if not self._feedback_ids_are_expected([feedback_id]):
                raise
        digest = self._payload_digest([feedback_id, helpful, task_outcome])
        DeferredTaskRepository(self.connection).defer(
            task_type="apply_retrieval_feedback",
            resource_type="feedback",
            resource_id=feedback_id,
            payload={
                "feedback_id": feedback_id,
                "helpful": helpful,
                "task_outcome": task_outcome,
                "created_at": created_at,
            },
            idempotency_key=f"apply_retrieval_feedback:{digest}",
            run_after=created_at,
            max_attempts=self.settings.recall_side_effect_max_attempts,
            error="waiting for recall exposure",
            updated_at=created_at,
        )
        return {"created": False, "updated": True}

    def mark_feedback_injected_eventually(self, feedback_ids: list[str], created_at: str) -> int:
        """exposure 尚未可见时持久登记 injected 标记并返回已接受数量。"""
        unique_ids = list(dict.fromkeys(feedback_ids))
        try:
            return self.repository.mark_feedback_injected_batch(unique_ids)
        except ValueError as error:
            if not str(error).startswith("feedback exposure not found:"):
                raise
            if not self._feedback_ids_are_expected(unique_ids):
                raise
        digest = self._payload_digest(sorted(unique_ids))
        DeferredTaskRepository(self.connection).defer(
            task_type="mark_recall_feedback_injected",
            resource_type="feedback",
            resource_id=unique_ids[0],
            payload={"feedback_ids": unique_ids},
            idempotency_key=f"mark_recall_feedback_injected:{digest}",
            run_after=created_at,
            max_attempts=self.settings.recall_side_effect_max_attempts,
            error="waiting for recall exposure",
            updated_at=created_at,
        )
        return len(unique_ids)

    @staticmethod
    def _payload_digest(payload: list[Any]) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _feedback_ids_are_expected(self, feedback_ids: list[str]) -> bool:
        repository = DeferredTaskRepository(self.connection)
        for feedback_id in feedback_ids:
            if (
                self.connection.execute(
                    "SELECT 1 FROM retrieval_feedback WHERE id=?",
                    (feedback_id,),
                ).fetchone()
                is not None
            ):
                continue
            if self.pending_exposure_check is not None and self.pending_exposure_check([feedback_id]):
                continue
            if repository.has_pending_recall_exposure(feedback_id):
                continue
            return False
        return True


__all__ = [
    "ExperienceService",
    "InvalidStateTransitionError",
    "backprop_episode_reward",
]
