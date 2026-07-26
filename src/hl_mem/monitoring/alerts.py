"""带去重、恢复通知与 expansion 熔断的告警状态机。"""

from __future__ import annotations

import time
from dataclasses import dataclass

from hl_mem.monitoring.channels import AlertChannel


@dataclass
class AlertState:
    """单个告警键的当前状态。"""

    severity: str
    active: bool
    last_sent_at: float


class AlertManager:
    """管理 WARNING/ERROR/CRITICAL 状态、五分钟去重和恢复通知。"""

    def __init__(self, channels: list[AlertChannel], dedupe_seconds: float = 300.0) -> None:
        self.channels = channels
        self.dedupe_seconds = dedupe_seconds
        self.states: dict[str, AlertState] = {}

    def transition(self, key: str, severity: str | None, detail: dict[str, object]) -> bool:
        """触发或恢复告警；返回是否实际发送通知。"""
        now = time.time()
        previous = self.states.get(key)
        if severity is None:
            if previous is None or not previous.active:
                return False
            self.states[key] = AlertState(previous.severity, False, now)
            return self._send(f"RECOVERED: {key}", detail)
        if severity not in {"WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("severity must be WARNING, ERROR, CRITICAL or None")
        if (
            previous
            and previous.active
            and previous.severity == severity
            and now - previous.last_sent_at < self.dedupe_seconds
        ):
            return False
        self.states[key] = AlertState(severity, True, now)
        return self._send(f"{severity}: {key}", detail)

    def _send(self, subject: str, detail: dict[str, object]) -> bool:
        for channel in self.channels:
            channel.send(subject, detail)
        return True


class CircuitBreaker:
    """连续失败达到阈值后临时阻断 query expansion。"""

    def __init__(self, failure_threshold: int = 5, open_seconds: float = 60.0) -> None:
        self.failure_threshold, self.open_seconds = failure_threshold, open_seconds
        self.failures = 0
        self.opened_at: float | None = None

    def allow(self) -> bool:
        """返回当前是否允许调用，并在冷却后半开。"""
        if self.opened_at is None:
            return True
        if time.time() - self.opened_at >= self.open_seconds:
            self.failures, self.opened_at = 0, None
            return True
        return False

    def record(self, success: bool) -> None:
        """记录结果并更新熔断状态。"""
        if success:
            self.failures, self.opened_at = 0, None
            return
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self.opened_at = time.time()
