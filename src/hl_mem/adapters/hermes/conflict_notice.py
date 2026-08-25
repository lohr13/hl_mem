"""Session-scoped residual conflict notices for the Hermes prompt header."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from hl_mem.adapters.hermes.http_client import HLMemHttpClient

logger = logging.getLogger(__name__)


class ManualConflictNotice:
    def __init__(self, enabled: bool, ttl_seconds: float) -> None:
        self.enabled = enabled
        self.ttl_seconds = ttl_seconds
        self.snapshot: dict[str, int] | None = None
        self.expires_at = 0.0
        self.failed = False
        self._last_notified_by_session: dict[str, int] = {}

    def render(
        self,
        client: HLMemHttpClient,
        can_call: Callable[[], bool],
        on_success: Callable[[], None],
        on_failure: Callable[[], None],
        session_id: str,
    ) -> str | None:
        if not self.enabled:
            self.failed = False
            return None
        now = time.monotonic()
        if self.snapshot is None or now >= self.expires_at:
            count = self._refresh(client, can_call, on_success, on_failure)
            if count is None:
                return None
            self.snapshot = {"manual_required_count": count}
            self.expires_at = now + self.ttl_seconds
        count = self.snapshot["manual_required_count"]
        session = session_id or "<process>"
        previous = self._last_notified_by_session.get(session)
        self._last_notified_by_session[session] = count
        if count <= 0 or previous == count:
            return None
        return f"hl_mem 仍有 {count} 个低置信真两难冲突未自动收敛；需要时可查看冲突列表。"

    def _refresh(
        self,
        client: HLMemHttpClient,
        can_call: Callable[[], bool],
        on_success: Callable[[], None],
        on_failure: Callable[[], None],
    ) -> int | None:
        if not can_call():
            self._discard()
            return None
        try:
            payload: Any = client.get("/healthz").json()
            count = int(payload["manual_required_count"])
            if count < 0:
                raise ValueError("manual_required_count must be non-negative")
            self.failed = False
            on_success()
            return count
        except Exception:
            self._discard()
            on_failure()
            logger.warning("Hermes conflict notice health read failed; stale count discarded", exc_info=True)
            return None

    def _discard(self) -> None:
        self.snapshot = None
        self.expires_at = 0.0
        self.failed = True
