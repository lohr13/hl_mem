"""Experience 应用服务。"""

from __future__ import annotations

from typing import Any

from hl_mem.storage.experience import (
    ExperienceRepository,
    InvalidStateTransitionError,
    backprop_episode_reward,
)


class ExperienceService(ExperienceRepository):
    """Experience 应用入口，协调 exposure 物化与 delivery receipt。"""

    def record_exposure_batch(self, exposures: list[tuple[Any, ...]]) -> int:
        """原子持久化最终 packet items 对应的 exposure。"""
        return super().record_exposure_batch(exposures)

    def mark_feedback_injected_batch(self, feedback_ids: list[str]) -> int:
        """原子标记已经交付到 Agent host/model 输入边界的 exposure。"""
        return super().mark_feedback_injected_batch(feedback_ids)


__all__ = [
    "ExperienceService",
    "InvalidStateTransitionError",
    "backprop_episode_reward",
]
