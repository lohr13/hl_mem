from __future__ import annotations

from typing import Any

from hl_mem.adapters.hermes.conflict_notice import ManualConflictNotice
from hl_mem.adapters.hermes.provider import HLMemProvider
from hl_mem.settings import Settings


class HealthResponse:
    def __init__(self, count: int) -> None:
        self.count = count

    def json(self) -> dict[str, int]:
        return {"manual_required_count": self.count}


class HealthClient:
    def __init__(self, count: int) -> None:
        self.count = count

    def get(self, path: str) -> HealthResponse:
        assert path == "/healthz"
        return HealthResponse(self.count)


def render_notice(notice: ManualConflictNotice, client: Any, session_id: str) -> str | None:
    return notice.render(client, lambda: True, lambda: None, lambda: None, session_id)


def test_manual_conflict_notice_evicts_oldest_sessions_at_hard_limit() -> None:
    notice = ManualConflictNotice(enabled=True, ttl_seconds=300.0)
    client = HealthClient(count=0)

    for index in range(257):
        assert render_notice(notice, client, f"session-{index}") is None

    assert len(notice._last_notified_by_session) == 256
    assert "session-0" not in notice._last_notified_by_session
    assert "session-256" in notice._last_notified_by_session


def test_on_session_end_forgets_notice_state_and_allows_same_session_notice_again(monkeypatch) -> None:
    provider = HLMemProvider(settings=Settings(hermes_enabled=True, hermes_manual_conflict_notice=True))
    monkeypatch.setattr(provider._client, "get", lambda _path: HealthResponse(1))
    provider.initialize("session-1")

    assert "1 个低置信真两难冲突" in provider.system_prompt_block()
    assert provider.system_prompt_block().count("低置信真两难冲突") == 0

    provider.on_session_end([], session_id="session-1")

    assert "1 个低置信真两难冲突" in provider.system_prompt_block()


def test_manual_conflict_notice_preserves_render_semantics() -> None:
    notice = ManualConflictNotice(enabled=True, ttl_seconds=300.0)
    positive_client = HealthClient(count=2)

    assert render_notice(notice, positive_client, "session-positive") == (
        "hl_mem 仍有 2 个低置信真两难冲突未自动收敛；需要时可查看冲突列表。"
    )
    assert render_notice(notice, positive_client, "session-positive") is None

    silent_notice = ManualConflictNotice(enabled=True, ttl_seconds=300.0)
    assert render_notice(silent_notice, HealthClient(count=0), "session-zero") is None
