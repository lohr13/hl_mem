"""旧 Worker token 预算接口到原子用量账本的临时兼容层。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import cast

from hl_mem.observability.usage import UsageAmount, UsageGovernor, UsageIdentity, UsageLimits
from hl_mem.plugins.contracts import ProviderCapability

_LEGACY_IDENTITY = UsageIdentity(
    ProviderCapability.LLM,
    "legacy_worker_budget",
    "hl-mem.builtin",
    "legacy",
    "legacy",
)


class TokenBudget:
    """Task 5 前保留的旧接口；持久化与原子结算由 UsageGovernor 负责。"""

    def __init__(
        self,
        daily_limit: int = 500_000,
        path: str | Path = "hl_mem_budget.db",
        today: Callable[[], date] = date.today,
    ) -> None:
        self.daily_limit = daily_limit
        self.path = Path(path)
        self._today = today
        self._governor = UsageGovernor(
            self.path,
            UsageLimits(0, daily_limit, 0),
            now=lambda: datetime.combine(self._today(), time.min, timezone.utc),
        )

    def can_spend(self, estimated_tokens: int) -> bool:
        """兼容旧的非预留检查；Task 5 会删除最后一个生产调用方。"""

        if estimated_tokens < 0:
            raise ValueError("estimated_tokens must be non-negative")
        remaining_values = cast(Mapping[str, object], self._governor.snapshot()["remaining"])
        remaining = cast(int, remaining_values["tokens"])
        return remaining < 0 or estimated_tokens <= remaining

    def record_usage(self, actual_tokens: int) -> None:
        """通过一次原子预留与结算记录旧 Worker token 用量。"""

        if actual_tokens < 0:
            raise ValueError("actual_tokens must be non-negative")
        amount = UsageAmount(input_tokens=actual_tokens)
        reservation = self._governor.reserve(_LEGACY_IDENTITY, UsageAmount())
        self._governor.settle(reservation.id, amount, status="legacy", latency_ms=0.0)

    def get_stats(self) -> dict[str, int | str]:
        """返回旧接口所需的今日 token 统计。"""

        snapshot = self._governor.snapshot()
        settled_values = cast(Mapping[str, object], snapshot["settled"])
        remaining_values = cast(Mapping[str, object], snapshot["remaining"])
        used = cast(int, settled_values["total_tokens"])
        remaining = cast(int, remaining_values["tokens"])
        return {
            "date": str(snapshot["date"]),
            "daily_limit": self.daily_limit,
            "used_tokens": used,
            "remaining_tokens": remaining,
        }
