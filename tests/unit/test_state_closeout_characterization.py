"""Pin the conservative v0.29.3 state behavior restored after the failed v0.30 experiment."""

from __future__ import annotations

import json
from typing import Any

from hl_mem.application.ingest import IngestService
from hl_mem.application.recall import is_access_recording_eligible
from hl_mem.domain.recall import RecallIntent
from hl_mem.ingest.chunking import ChunkingPolicy
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.ingest.llm_extractor import LLMExtractor
from hl_mem.llm.types import LLMRequest, LLMResponse
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database


class _FakeLLMClient:
    class _Provider:
        name = "fake"

    provider = _Provider()
    model = "test-model"

    def __init__(self, response: dict[str, object]) -> None:
        self.response = response

    def complete(self, request: LLMRequest, *, timeout_seconds: float | None = None) -> LLMResponse:
        del request, timeout_seconds
        return LLMResponse(json.dumps(self.response, ensure_ascii=False), "stop", 1)


def _store_version(connection: Any, event_id: str, occurred_at: str, value: str) -> str:
    result = IngestService.store_extracted(
        connection,
        ExtractedClaim(
            predicate="配置",
            value=value,
            subject="X",
            canonical_attribute="config.version",
            assertion_kind="observation",
        ),
        {
            "id": event_id,
            "actor_type": "user",
            "tenant_id": "default",
            "occurred_at": occurred_at,
        },
        occurred_at,
        FakeEmbedder(8),
    )
    assert result.claim_id is not None
    return result.claim_id


def test_product_extractor_rejects_operational_version_snapshot() -> None:
    value = "hl_mem 当前版本为 v0.30.0"
    response: dict[str, object] = {
        "claims": [
            {
                "subject": "hl_mem",
                "value": value,
                "kind": "config",
                "confidence": 0.99,
                "notability": "high",
                "assertion_kind": "observation",
                "evidence_quote": value,
                "source_event_indices": [0],
            }
        ],
        "should_memorize": True,
    }

    claims = LLMExtractor(_FakeLLMClient(response), ChunkingPolicy(10_000, 0, 2)).extract(value)

    assert claims == []


def test_version_updates_remain_visible_and_do_not_create_state_edge(tmp_path: Any) -> None:
    connection = Database(tmp_path / "conservative-version.db").open()
    old_id = _store_version(connection, "version-old", "2026-08-20T08:00:00+00:00", "X版本为0.1")
    new_id = _store_version(connection, "version-new", "2026-08-21T08:00:00+00:00", "X版本为0.2")

    repository = ClaimRepository(connection)
    old = repository.get_claim(old_id)
    new = repository.get_claim(new_id)

    assert (old["status"], old["valid_to"], old["superseded_by_id"]) == ("active", None, None)
    assert (new["status"], new["supersedes_id"]) == ("active", None)


def test_historical_recall_keeps_v0293_access_refresh_behavior() -> None:
    assert is_access_recording_eligible(
        intent=RecallIntent.HISTORICAL,
        as_of=None,
        known_as_of=None,
    )
