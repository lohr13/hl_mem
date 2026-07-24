"""Experience 应用服务。"""

from __future__ import annotations

from hl_mem.storage.experience import (
    ExperienceRepository,
    InvalidStateTransitionError,
    backprop_episode_reward,
)


class ExperienceService(ExperienceRepository):
    """兼容的应用层名称；当前仍被 API、worker 与外部调用方广泛使用。"""


__all__ = ["ExperienceService", "InvalidStateTransitionError", "backprop_episode_reward"]
