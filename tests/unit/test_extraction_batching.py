"""有界提取微批的调度、来源映射与生产路径回归。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from hl_mem.api.server import create_app
from hl_mem.application.ingest import IngestService
from hl_mem.errors import ConfigurationError
from hl_mem.ingest.chunking import ChunkingPolicy
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.ingest.llm_extractor import LLMExtractor
from hl_mem.llm.types import LLMRequest, LLMResponse
from hl_mem.settings import Settings
from hl_mem.storage.database import Database
from hl_mem.storage.events import EventRepository
from hl_mem.storage.evidence import EvidenceRepository
from hl_mem.storage.jobs import JobRepository
from hl_mem.workers.worker import Worker

BASE_TIME = datetime(2026, 8, 10, tzinfo=timezone.utc)


class _FakeLLMClient:
    class _Provider:
        name = "fake"

    provider = _Provider()
    model = "test-model"

    def __init__(self, content: str) -> None:
        self.content = content
        self.last_request: LLMRequest | None = None

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.last_request = request
        return LLMResponse(self.content, "stop", 12, input_tokens=10, output_tokens=2)


class _UnlimitedBudget:
    def can_spend(self, _tokens: int) -> bool:
        return True

    def record_usage(self, _tokens: int) -> None:
        return None

    def get_stats(self) -> dict[str, int]:
        return {"used": 0, "limit": 0, "remaining": 0}


def _batch_source_events() -> list[dict[str, Any]]:
    return [
        {
            "event_index": 0,
            "actor_type": "user",
            "turn": "7",
            "occurred_at": "2026-08-09T10:00:00+00:00",
            "content": {"text": "用户喜欢红茶"},
        },
        {
            "event_index": 1,
            "actor_type": "assistant",
            "turn": "7",
            "occurred_at": "2026-08-09T10:00:01+00:00",
            "content": {"text": "已采用 SQLite WAL 架构"},
        },
    ]


def _batch_content() -> dict[str, Any]:
    return {
        "messages": [
            {
                "event_index": item["event_index"],
                "speaker": item["actor_type"],
                "turn": item["turn"],
                "occurred_at": item["occurred_at"],
                "content": item["content"]["text"],
            }
            for item in _batch_source_events()
        ]
    }


def _compact_response(*, indices: list[int] | None, quote: str = "用户喜欢红茶") -> str:
    claim: dict[str, Any] = {
        "subject": "用户",
        "value": "用户喜欢红茶",
        "kind": "preference",
        "confidence": 1.0,
        "notability": "high",
        "evidence_quote": quote,
    }
    if indices is not None:
        claim["source_event_indices"] = indices
    return json.dumps({"claims": [claim], "should_memorize": True}, ensure_ascii=False)


def _iso(seconds: float) -> str:
    return (BASE_TIME + timedelta(seconds=seconds)).isoformat()


def _insert_event_job(
    connection: Any,
    index: int,
    *,
    namespace: str = "default",
    session_id: str | None = "session-1",
    event_type: str = "message",
    actor_type: str = "user",
    recorded_seconds: float = 0,
) -> tuple[str, str]:
    event_id = f"event-{index}"
    job_id = f"job-{index}"
    EventRepository(connection).insert_event(
        {
            "id": event_id,
            "tenant_id": namespace,
            "session_id": session_id,
            "event_type": event_type,
            "actor_type": actor_type,
            "content": {"text": f"message {index}"},
            "occurred_at": _iso(recorded_seconds),
            "recorded_at": _iso(recorded_seconds),
            "sensitivity": "normal",
        }
    )
    JobRepository(connection).insert_job(
        {
            "id": job_id,
            "job_type": "extract_event",
            "payload": {"event_id": event_id},
            "idempotency_key": f"extract:{event_id}",
            "created_at": _iso(recorded_seconds),
            "updated_at": _iso(recorded_seconds),
        }
    )
    return event_id, job_id


def _lease_batch(
    jobs: JobRepository,
    *,
    now_seconds: float,
    force: bool = False,
) -> dict[str, Any] | None:
    return jobs.lease_job(
        _iso(now_seconds + 300),
        _iso(now_seconds),
        extraction_batch_max_events=4,
        extraction_batch_max_wait_seconds=2.0,
        force_extraction=force,
    )


def test_settings_expose_only_two_microbatch_controls() -> None:
    settings = Settings.for_test()

    assert settings.extraction_batch_max_events == 5
    assert settings.extraction_batch_max_wait_seconds == 120.0
    assert not hasattr(settings, "extraction_batch_idle_seconds")

    replace(settings, extraction_batch_max_events=32).validate()
    with pytest.raises(ConfigurationError, match="between 1 and 32"):
        replace(settings, extraction_batch_max_events=33).validate()
    with pytest.raises(ConfigurationError, match="worker.job_lease_minutes must be positive"):
        replace(settings, worker_job_lease_minutes=0).validate()


def test_lease_groups_four_ordered_events_from_one_session(tmp_path) -> None:
    connection = Database(tmp_path / "full-window.db").open()
    jobs = JobRepository(connection)
    for index, actor in enumerate(("user", "assistant", "user", "assistant")):
        _insert_event_job(connection, index, actor_type=actor, recorded_seconds=index / 10)
    _insert_event_job(connection, 4, session_id="other-session")
    _insert_event_job(connection, 5, namespace="other-namespace")

    leased = _lease_batch(jobs, now_seconds=0.5)

    assert leased is not None
    assert leased["payload"] == {"event_ids": ["event-0", "event-1", "event-2", "event-3"]}
    assert leased["leased_job_ids"] == ["job-0", "job-1", "job-2", "job-3"]
    pending = connection.execute("SELECT id FROM jobs WHERE status='pending' ORDER BY id").fetchall()
    assert [row["id"] for row in pending] == ["job-4", "job-5"]


def test_partial_window_waits_until_oldest_event_reaches_max_wait(tmp_path) -> None:
    connection = Database(tmp_path / "max-wait.db").open()
    jobs = JobRepository(connection)
    _insert_event_job(connection, 0)
    _insert_event_job(connection, 1, actor_type="assistant", recorded_seconds=0.1)

    assert _lease_batch(jobs, now_seconds=1.999) is None

    leased = _lease_batch(jobs, now_seconds=2.0)
    assert leased is not None
    assert leased["payload"]["event_ids"] == ["event-0", "event-1"]


def test_force_flush_leases_a_young_partial_window(tmp_path) -> None:
    connection = Database(tmp_path / "force.db").open()
    jobs = JobRepository(connection)
    _insert_event_job(connection, 0)

    leased = _lease_batch(jobs, now_seconds=0.1, force=True)

    assert leased is not None
    assert leased["payload"] == {"event_ids": ["event-0"]}


def test_non_message_and_sessionless_events_remain_fast_lane(tmp_path) -> None:
    connection = Database(tmp_path / "fast-lane.db").open()
    jobs = JobRepository(connection)
    _insert_event_job(connection, 0, event_type="explicit_memory")
    _insert_event_job(connection, 1, session_id=None)

    first = _lease_batch(jobs, now_seconds=0.1)
    second = _lease_batch(jobs, now_seconds=0.1)

    assert first is not None and first["payload"] == {"event_ids": ["event-0"]}
    assert second is not None and second["payload"] == {"event_ids": ["event-1"]}


def test_bulk_completion_and_failure_cover_every_leased_job(tmp_path) -> None:
    connection = Database(tmp_path / "terminal.db").open()
    jobs = JobRepository(connection)
    for index in range(4):
        _insert_event_job(connection, index)
    leased = _lease_batch(jobs, now_seconds=0.1)
    assert leased is not None

    assert jobs.complete_jobs(leased["leased_job_ids"], _iso(1), leased["lease_token"]) == 4
    assert {row["status"] for row in connection.execute("SELECT status FROM jobs ORDER BY id").fetchall()} == {
        "succeeded"
    }

    for index in range(4, 8):
        _insert_event_job(connection, index, session_id="session-2")
    failed = _lease_batch(jobs, now_seconds=0.1)
    assert failed is not None
    assert jobs.fail_jobs(failed["leased_job_ids"], "boom", _iso(1), failed["lease_token"]) == 4
    rows = connection.execute("SELECT status,last_error FROM jobs WHERE id>='job-4' ORDER BY id").fetchall()
    assert [(row["status"], row["last_error"]) for row in rows] == [("pending", "boom")] * 4


def test_batch_ingest_is_atomic_when_second_job_enqueue_fails(tmp_path, monkeypatch) -> None:
    app = create_app(replace(Settings.for_test(), database_path=str(tmp_path / "atomic-api.db")))
    original = IngestService._queue_event
    calls = 0

    def fail_second(self: IngestService, event_id: str, now: str, commit: bool = False) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("second enqueue failed")
        original(self, event_id, now, commit)

    monkeypatch.setattr(IngestService, "_queue_event", fail_second)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/v1/events/batch",
            json={
                "events": [
                    {"session_id": "s", "actor_type": "user", "content": {"text": "u"}},
                    {"session_id": "s", "actor_type": "assistant", "content": {"text": "a"}},
                ]
            },
        )

    assert response.status_code == 500
    connection = app.state.db.open()
    assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_batch_ingest_preserves_metadata_and_existing_job_contract(tmp_path) -> None:
    app = create_app(replace(Settings.for_test(), database_path=str(tmp_path / "batch-api.db")))
    payload = {
        "events": [
            {
                "id": "u1",
                "idempotency_key": "turn:u1",
                "session_id": "s",
                "actor_type": "user",
                "metadata": {"turn_id": "turn-1"},
                "content": {"text": "user one"},
            },
            {
                "id": "a1",
                "idempotency_key": "turn:a1",
                "session_id": "s",
                "actor_type": "assistant",
                "metadata": {"turn_id": "turn-1"},
                "content": {"text": "assistant one"},
            },
            {
                "id": "u2",
                "idempotency_key": "turn:u2",
                "session_id": "s",
                "actor_type": "user",
                "metadata": {"turn_id": "turn-2"},
                "content": {"text": "user two"},
            },
            {
                "id": "a2",
                "idempotency_key": "turn:a2",
                "session_id": "s",
                "actor_type": "assistant",
                "metadata": {"turn_id": "turn-2"},
                "content": {"text": "assistant two"},
            },
        ]
    }
    with TestClient(app) as client:
        response = client.post("/v1/events/batch", json=payload)

    assert response.status_code == 200
    assert response.json() == {
        "events": [
            {"id": "u1", "created": True},
            {"id": "a1", "created": True},
            {"id": "u2", "created": True},
            {"id": "a2", "created": True},
        ]
    }
    connection = app.state.db.open()
    rows = connection.execute("SELECT id,metadata_json FROM events ORDER BY recorded_at,id").fetchall()
    assert [(row["id"], json.loads(row["metadata_json"])["turn_id"]) for row in rows] == [
        ("u1", "turn-1"),
        ("a1", "turn-1"),
        ("u2", "turn-2"),
        ("a2", "turn-2"),
    ]
    jobs = connection.execute("SELECT job_type,payload_json FROM jobs ORDER BY id").fetchall()
    assert {row["job_type"] for row in jobs} == {"extract_event"}
    assert {json.loads(row["payload_json"])["event_id"] for row in jobs} == {"u1", "a1", "u2", "a2"}

    leased = JobRepository(connection).lease_job(
        _iso(300),
        _iso(1),
        extraction_batch_max_events=4,
        extraction_batch_max_wait_seconds=0,
        force_extraction=True,
    )
    assert leased is not None
    assert leased["payload"] == {"event_ids": ["u1", "a1", "u2", "a2"]}


def test_llm_claim_keeps_valid_source_event_indices() -> None:
    client = _FakeLLMClient(_compact_response(indices=[0]))
    extractor = LLMExtractor(client, ChunkingPolicy(10_000, 0, 2))

    claims = extractor.extract(
        _batch_content(),
        {"_source_events": _batch_source_events(), "session_id": "session-1"},
    )

    assert len(claims) == 1
    assert claims[0].source_event_indices == (0,)


def test_llm_rejects_missing_or_out_of_range_indices_for_multi_event_source() -> None:
    for indices in (None, [2]):
        extractor = LLMExtractor(
            _FakeLLMClient(_compact_response(indices=indices)),
            ChunkingPolicy(10_000, 0, 2),
        )

        assert (
            extractor.extract(
                _batch_content(),
                {"_source_events": _batch_source_events()},
            )
            == []
        )


def test_llm_rejects_quote_not_found_in_declared_source_event() -> None:
    extractor = LLMExtractor(
        _FakeLLMClient(_compact_response(indices=[1], quote="用户喜欢红茶")),
        ChunkingPolicy(10_000, 0, 2),
    )

    assert (
        extractor.extract(
            _batch_content(),
            {"_source_events": _batch_source_events()},
        )
        == []
    )


def test_chunk_dedup_unions_source_event_indices() -> None:
    first = ExtractedClaim(predicate="preference", value="用户喜欢红茶", source_event_indices=(0,))
    second = ExtractedClaim(predicate="preference", value="用户喜欢红茶", source_event_indices=(1,))

    merged = LLMExtractor._merge_chunk_claims([[first], [second]])

    assert len(merged) == 1
    assert merged[0].source_event_indices == (0, 1)


def test_store_extracted_links_every_declared_source_event(tmp_path) -> None:
    connection = Database(tmp_path / "multi-evidence.db").open()
    service = IngestService(connection)
    for event_id, actor in (("event-user", "user"), ("event-assistant", "assistant")):
        service.ingest_event(
            {
                "id": event_id,
                "session_id": "session-1",
                "event_type": "message",
                "actor_type": actor,
                "content": {"text": "用户喜欢红茶" if actor == "user" else "确认这个偏好"},
                "occurred_at": _iso(0),
            }
        )
    repository = EventRepository(connection)
    sources = [repository.get_event("event-user"), repository.get_event("event-assistant")]
    assert all(source is not None for source in sources)
    claim = ExtractedClaim(
        predicate="preference",
        value="用户喜欢红茶",
        subject="用户",
        confidence=1.0,
        importance=0.9,
        source_event_indices=(0, 1),
    )

    stored = IngestService.store_extracted(
        connection,
        claim,
        sources[0],
        _iso(1),
        FakeEmbedder(),
        source_events=sources,
        policy=Settings.for_test().retention_policy(),
    )

    assert stored.claim_id is not None
    links = EvidenceRepository(connection).get_links_for_derived("claim", stored.claim_id)
    assert {link["evidence_id"] for link in links if link["relation"] == "derived_from"} == {
        "event-user",
        "event-assistant",
    }


def test_worker_extracts_one_structured_turn_and_persists_per_event_evidence(tmp_path) -> None:
    database_path = tmp_path / "worker-batch.db"
    connection = Database(database_path).open()
    service = IngestService(connection)
    service.ingest_events(
        [
            {
                "id": "event-user",
                "session_id": "session-1",
                "event_type": "message",
                "actor_type": "user",
                "metadata": {"turn_id": "7"},
                "content": {"text": "用户喜欢红茶"},
                "occurred_at": _iso(0),
            },
            {
                "id": "event-assistant",
                "session_id": "session-1",
                "event_type": "message",
                "actor_type": "assistant",
                "metadata": {"turn_id": "7"},
                "content": {"text": "已采用 SQLite WAL 架构"},
                "occurred_at": _iso(1),
            },
        ]
    )
    response = json.dumps(
        {
            "claims": [
                {
                    "subject": "用户",
                    "value": "用户喜欢红茶",
                    "kind": "preference",
                    "confidence": 1.0,
                    "notability": "high",
                    "evidence_quote": "用户喜欢红茶",
                    "source_event_indices": [0],
                },
                {
                    "subject": "hl_mem",
                    "value": "hl_mem 采用 SQLite WAL 架构",
                    "kind": "architecture",
                    "confidence": 1.0,
                    "notability": "high",
                    "evidence_quote": "采用 SQLite WAL 架构",
                    "source_event_indices": [1],
                },
            ],
            "should_memorize": True,
        },
        ensure_ascii=False,
    )
    client = _FakeLLMClient(response)
    extractor = LLMExtractor(client, ChunkingPolicy(10_000, 0, 2))
    worker = Worker(
        replace(Settings.for_test(), database_path=str(database_path)),
        connection=connection,
        extractor=extractor,
        embedder=FakeEmbedder(),
        image_describer=None,
        budget=_UnlimitedBudget(),
    )

    result = worker.run_once(force_extraction=True)

    assert result["status"] == "succeeded"
    assert result["events"] == 2
    assert result["claims"] == 2
    assert result["total_tokens"] == 12
    assert client.last_request is not None
    prompt = client.last_request.messages[1].content
    assert '"event_index": 0' in prompt
    assert '"speaker": "user"' in prompt
    assert '"turn": "7"' in prompt
    assert '"speaker": "assistant"' in prompt
    assert "_source_events" not in prompt
    rows = connection.execute("SELECT id,value_json FROM claims ORDER BY id").fetchall()
    evidence = EvidenceRepository(connection)
    sources_by_value = {
        json.loads(row["value_json"]): {
            link["evidence_id"]
            for link in evidence.get_links_for_derived("claim", row["id"])
            if link["relation"] == "derived_from"
        }
        for row in rows
    }
    assert sources_by_value["用户喜欢红茶"] == {"event-user"}
    assert sources_by_value["hl_mem 采用 SQLite WAL 架构"] == {"event-assistant"}
    assert worker.run_once(force_extraction=True) == {"status": "idle"}


def test_worker_filters_each_event_before_building_batch(tmp_path) -> None:
    database_path = tmp_path / "worker-filter.db"
    connection = Database(database_path).open()
    IngestService(connection).ingest_events(
        [
            {
                "id": "event-user",
                "session_id": "session-1",
                "event_type": "message",
                "actor_type": "user",
                "content": {"text": "用户喜欢红茶"},
            },
            {
                "id": "event-assistant",
                "session_id": "session-1",
                "event_type": "message",
                "actor_type": "assistant",
                "content": {"text": "好的"},
            },
        ]
    )
    client = _FakeLLMClient(_compact_response(indices=[0]))
    worker = Worker(
        replace(Settings.for_test(), database_path=str(database_path)),
        connection=connection,
        extractor=LLMExtractor(client, ChunkingPolicy(10_000, 0, 2)),
        embedder=FakeEmbedder(),
        image_describer=None,
        budget=_UnlimitedBudget(),
    )

    result = worker.run_once(force_extraction=True)

    assert result["events"] == 2
    assert result["eligible_events"] == 1
    assert client.last_request is not None
    assert '"speaker": "assistant"' not in client.last_request.messages[1].content
