from __future__ import annotations

from dataclasses import replace

from hl_mem.application.ingest import IngestService
from hl_mem.application.recall import RecallService
from hl_mem.domain.temporal import RecallIntent
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.settings import Settings
from hl_mem.storage.database import Database


def _snapshot(value: str, *, assertion_kind: str) -> ExtractedClaim:
    return ExtractedClaim(
        predicate="事实",
        value=value,
        subject="小满",
        canonical_attribute="fact.other",
        assertion_kind=assertion_kind,  # type: ignore[arg-type]
    )


def _store(connection, value: str, event_id: str, occurred_at: str, *, assertion_kind: str):
    return IngestService.store_extracted(
        connection,
        _snapshot(value, assertion_kind=assertion_kind),
        {
            "id": event_id,
            "tenant_id": "default",
            "actor_type": "assistant",
            "occurred_at": occurred_at,
        },
        occurred_at,
        FakeEmbedder(8),
    )


def test_a2_closed_three_snapshot_chain_injects_only_current_but_keeps_history(tmp_path) -> None:
    connection = Database(tmp_path / "a3-current-state.db").open()
    offline = _store(
        connection,
        "小满已离线 7 天",
        "offline-seven-days",
        "2026-08-16T06:47:22+00:00",
        assertion_kind="unknown",
    )
    repeated = _store(
        connection,
        "小满当前处于离线状态",
        "offline-current",
        "2026-08-16T08:10:21+00:00",
        assertion_kind="observation",
    )
    online = _store(
        connection,
        "在线",
        "online-current",
        "2026-08-17T08:27:42+00:00",
        assertion_kind="observation",
    )
    assert offline.claim_id is not None and online.claim_id is not None
    assert repeated.claim_id == offline.claim_id

    settings = replace(
        Settings.for_test(),
        recall_dense_enabled=False,
        resurrection_mode="off",
    )
    service = RecallService(connection, FakeEmbedder(8), settings=settings)
    current = service.recall(
        "小满",
        limit=10,
        as_of="2026-08-18T00:00:00+00:00",
        intent=RecallIntent.CURRENT_STATE,
        context_mode="packed",
        response_format="both",
        token_budget=1000,
        ranking_now="2026-08-18T00:00:00+00:00",
    )
    historical = service.recall(
        "小满",
        limit=10,
        as_of="2026-08-18T00:00:00+00:00",
        intent=RecallIntent.HISTORICAL,
        response_format="retrieval_bundle",
        token_budget=1000,
        ranking_now="2026-08-18T00:00:00+00:00",
    )

    assert [item["id"] for item in current["results"]] == [online.claim_id]
    assert [item["id"] for item in current["context_packet"]["items"]] == [online.claim_id]
    assert [item["data"]["id"] for item in current["context"]["context_items"]] == [online.claim_id]
    historical_ids = {item["id"] for item in historical["retrieval_bundle"]["items"]}
    assert {offline.claim_id, online.claim_id} <= historical_ids
