"""Experience 应用服务。"""

from __future__ import annotations

from hl_mem.storage.experience import (
    ExperienceRepository,
    InvalidStateTransitionError,
    backprop_episode_reward,
)


class ExperienceService(ExperienceRepository):
    """将 Experience 用例委托给仓储实现。"""


__all__ = ["ExperienceService", "InvalidStateTransitionError", "backprop_episode_reward"]
