"""Experience 应用服务。"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

from hl_mem.storage.experience import (
    ExperienceRepository,
    InvalidStateTransitionError,
    backprop_episode_reward,
)

if TYPE_CHECKING:
    from hl_mem.settings import Settings


class ExperienceService:
    """Experience 应用入口，协调 exposure 物化与 delivery receipt。"""

    def __init__(
        self,
        connection: sqlite3.Connection,
        min_support: int = 2,
        retire_after_failures: int = 3,
        settings: Settings | None = None,
    ) -> None:
        self.repository = ExperienceRepository(
            connection,
            min_support=min_support,
            retire_after_failures=retire_after_failures,
            settings=settings,
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


__all__ = [
    "ExperienceService",
    "InvalidStateTransitionError",
    "backprop_episode_reward",
]
