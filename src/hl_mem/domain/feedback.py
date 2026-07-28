"""反馈 usefulness 的纯领域策略。"""

from __future__ import annotations

from dataclasses import dataclass
from math import floor


@dataclass(frozen=True)
class BayesianUsefulnessPolicy:
    """使用贝叶斯平滑计算有界 usefulness 与正向保留奖励。"""

    bonus_every: int = 3
    bonus_days: int = 14
    max_bonus_days: int = 180

    def __post_init__(self) -> None:
        if self.bonus_every <= 0 or self.bonus_days < 0 or self.max_bonus_days < 0:
            raise ValueError("feedback bonus settings must be non-negative and bonus_every must be positive")

    def evaluate(
        self,
        *,
        helpful_count: int,
        unhelpful_count: int,
        success_sum: float,
        outcome_count: int,
    ) -> tuple[float, int]:
        """返回平滑 usefulness 分数与仅由正证据产生的奖励天数。"""
        if min(helpful_count, unhelpful_count, outcome_count) < 0 or not 0.0 <= success_sum <= outcome_count:
            raise ValueError("feedback aggregates are outside their valid ranges")
        helpful_rate = (helpful_count + 2) / (helpful_count + unhelpful_count + 4)
        success_rate = (success_sum + 1) / (outcome_count + 2)
        usefulness = 0.7 * helpful_rate + 0.3 * success_rate
        positive_evidence = helpful_count + floor(success_sum)
        bonus = floor(positive_evidence / self.bonus_every) * self.bonus_days
        return usefulness, min(self.max_bonus_days, bonus)
