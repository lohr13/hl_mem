import threading

import httpx

from hl_mem.adapters.hermes.prefetch import PrefetchCache
from hl_mem.adapters.hermes.provider import (
    MAX_DELIVERY_RECEIPTS,
    HLMemProvider,
    _memory_idempotency_key,
    _summarize_observation,
    _validation_response_body,
)
from hl_mem.application.context_packet import retrieval_bundle_to_dict
from hl_mem.settings import Settings


class Response:
    def raise_for_status(self):
        pass

    def json(self):
        return {"results": [{"id": "one"}]}


class JsonResponse(Response):
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def _bundle_payload(query_id="query-1", texts=("cached memory",)):
    return {
        "schema_major": 1,
        "schema_minor": 0,
        "query_id": query_id,
        "answerability": "supported",
        "items": [
            {
                "type": "claim",
                "id": f"claim-{index}",
                "text": text,
                "evidence": [{"type": "event", "id": f"event-{index}"}],
                "score": 1.0 - index / 10,
            }
            for index, text in enumerate(texts, 1)
        ],
        "used_tokens_estimate": sum(max(1, (len(text) + 1) // 2) for text in texts),
        "truncated": False,
    }


def _packet_payload(bundle, feedback_prefix="feedback"):
    return {
        "schema_major": 1,
        "schema_minor": 0,
        "query_id": bundle["query_id"],
        "answerability": bundle["answerability"],
        "feedback_state": "available",
        "items": [
            {
                "type": item["type"],
                "id": item["id"],
                "text": item["text"],
                "evidence": item["evidence"],
                "feedback_id": f"{feedback_prefix}-{index}",
            }
            for index, item in enumerate(bundle["items"], 1)
        ],
        "used_tokens_estimate": bundle["used_tokens_estimate"],
        "truncated": bundle["truncated"],
    }


class AsyncClient:
    calls = 0
    requests = []
    error = None

    def __init__(self, **kwargs):
        self.timeout = kwargs["timeout"]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, **kwargs):
        type(self).calls += 1
        type(self).requests.append((url, kwargs.get("json")))
        if self.error:
            raise self.error
        if url.endswith("/v1/episodes"):
            return EpisodeResponse()
        return Response()

    async def patch(self, url, **kwargs):
        type(self).requests.append((url, kwargs.get("json")))
        return Response()


class EpisodeResponse(Response):
    def json(self):
        return {"id": "episode-1"}


def test_summarize_observation_detects_strong_error_signals() -> None:
    assert _summarize_observation('{"exit_code": 0, "output": "completed"}').startswith("[success]")
    assert _summarize_observation('{"exit_code": 1, "output": "stopped"}').startswith("[error]")
    assert _summarize_observation("Traceback (most recent call last):\nValueError: invalid").startswith("[error]")
    assert _summarize_observation("Error: something").startswith("[error]")


def test_summarize_observation_ignores_ambiguous_error_text() -> None:
    assert _summarize_observation("No errors found").startswith("[success]")
    assert _summarize_observation("The error counter is informational").startswith("[success]")
    assert _summarize_observation('{"result": "ok", "error_count": 0}').startswith("[success]")
    assert _summarize_observation("") == ""


def test_sync_hooks_post_payloads_and_report_success(monkeypatch) -> None:
    requests = []

    def post(url, **kwargs):
        requests.append((url, kwargs))
        return Response()

    monkeypatch.setattr(httpx, "post", post)
    provider = HLMemProvider("unused.db", "http://memory.test/", timeout=2.0)

    provider.on_memory_write("save", "memory", "喜欢黑咖啡")
    provider.on_pre_compress([{"role": "user", "content": "记住这个偏好"}])

    assert requests == [
        (
            "http://memory.test/v1/memories",
            {
                "json": {
                    "text": "喜欢黑咖啡",
                    "qualifiers": {"action": "save", "target": "memory"},
                    "idempotency_key": _memory_idempotency_key(
                        "save",
                        "memory",
                        "喜欢黑咖啡",
                    ),
                    "namespace": "default",
                },
                "timeout": 2.0,
            },
        ),
        (
            "http://memory.test/v1/events",
            {
                "json": {
                    "event_type": "message",
                    "actor_type": "user",
                    "content": {"text": "记住这个偏好"},
                    "namespace": "default",
                },
                "timeout": 2.0,
            },
        ),
    ]
    assert provider._failure_count == 0


def test_memory_write_uses_stable_key_and_trusted_namespace(monkeypatch) -> None:
    requests = []

    def post(url, **kwargs):
        requests.append((url, kwargs["json"]))
        return Response()

    monkeypatch.setattr(httpx, "post", post)
    provider = HLMemProvider("unused.db", "http://memory.test/", timeout=2.0)

    provider.on_memory_write(
        "save",
        "profile",
        "喜欢黑咖啡",
        namespace="project-a",
    )
    provider.on_memory_write(
        "save",
        "profile",
        "喜欢黑咖啡",
        namespace="project-a",
    )

    assert requests[0] == requests[1]
    assert requests[0][1] == {
        "text": "喜欢黑咖啡",
        "qualifiers": {"action": "save", "target": "profile"},
        "idempotency_key": _memory_idempotency_key(
            "save",
            "profile",
            "喜欢黑咖啡",
            "project-a",
        ),
        "namespace": "project-a",
    }
    assert _memory_idempotency_key(
        "save",
        "profile",
        "喜欢黑咖啡",
    ) != _memory_idempotency_key(
        "save",
        "profile",
        "喜欢拿铁",
    )
    assert _memory_idempotency_key(
        "save",
        "profile",
        "喜欢黑咖啡",
        "project-a",
    ) != _memory_idempotency_key(
        "save",
        "profile",
        "喜欢黑咖啡",
        "project-b",
    )


def test_write_hooks_propagate_only_trusted_host_namespace(monkeypatch) -> None:
    requests = []

    def post(url, **kwargs):
        requests.append((url, kwargs["json"]))
        return Response()

    monkeypatch.setattr(httpx, "post", post)
    provider = HLMemProvider("unused.db", "http://memory.test/", timeout=2.0)

    provider.sync_turn(
        "用户消息",
        "助手消息",
        namespace="project-a",
    )
    provider.on_pre_compress(
        [{"role": "user", "content": "压缩前", "namespace": "message-controlled"}],
        namespace="project-a",
    )
    provider.on_delegation(
        "委派任务",
        "委派结果",
        namespace="project-a",
    )

    event_payloads = [payload for url, payload in requests if url.endswith("/v1/events")]
    batch_payloads = [payload for url, payload in requests if url.endswith("/v1/events/batch")]
    assert len(event_payloads) == 3
    assert len(batch_payloads) == 1
    assert {payload["namespace"] for payload in event_payloads} == {"project-a"}
    assert {event["namespace"] for event in batch_payloads[0]["events"]} == {"project-a"}


def test_sync_turn_posts_one_atomic_pair_with_shared_turn_metadata(monkeypatch) -> None:
    requests = []

    def post(url, **kwargs):
        requests.append((url, kwargs["json"]))
        return Response()

    monkeypatch.setattr(httpx, "post", post)
    provider = HLMemProvider("unused.db", "http://memory.test/", timeout=2.0)

    provider.sync_turn(
        "用户消息",
        "助手消息",
        session_id="session-1",
        namespace="project-a",
        turn_id=7,
    )

    assert requests == [
        (
            "http://memory.test/v1/events/batch",
            {
                "events": [
                    {
                        "event_type": "message",
                        "actor_type": "user",
                        "content": {"text": "用户消息"},
                        "session_id": "session-1",
                        "namespace": "project-a",
                        "metadata": {"turn_id": "7"},
                        "idempotency_key": "hermes-turn:session-1:7:user",
                    },
                    {
                        "event_type": "message",
                        "actor_type": "assistant",
                        "content": {"text": "助手消息"},
                        "session_id": "session-1",
                        "namespace": "project-a",
                        "metadata": {"turn_id": "7"},
                        "idempotency_key": "hermes-turn:session-1:7:assistant",
                    },
                ]
            },
        )
    ]


def test_provider_uses_configurable_default_timeout() -> None:
    provider = HLMemProvider(settings=Settings(hermes_timeout=25))

    assert provider.timeout == 25.0
    assert provider._client.timeout == 25.0


def test_provider_defaults_to_long_recall_timeout() -> None:
    provider = HLMemProvider()

    assert provider.timeout == 30.0


def test_sync_hooks_open_circuit_after_repeated_http_failures(monkeypatch) -> None:
    calls = 0

    def post(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("unavailable")

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr("hl_mem.http_utils.time.sleep", lambda _delay: None)
    provider = HLMemProvider()

    for _ in range(6):
        provider.on_memory_write("save", "memory", "value")

    assert calls == 15
    assert provider._circuit_open_until > 0


def test_prefetch_success_timeout_and_circuit(monkeypatch) -> None:
    calls = 0
    error = None
    receipt_count = 0

    def post(url, **kwargs):
        nonlocal calls, receipt_count
        calls += 1
        if url.endswith("/v1/internal/retrieval-bundles"):
            assert "headers" not in kwargs
            if error:
                raise error
            return JsonResponse({"retrieval_bundle": _bundle_payload(f"query-{kwargs['json']['query']}")})
        if url.endswith("/v1/internal/context-packets/materialize"):
            receipt_count += 1
            bundle = kwargs["json"]["retrieval_bundle"]
            return JsonResponse({"context_packet": _packet_payload(bundle, f"feedback-{receipt_count}")})
        if url.endswith("/v1/internal/retrieval-feedback/injected"):
            return JsonResponse({"updated": len(kwargs["json"]["feedback_ids"])})
        raise AssertionError(f"unexpected request: {url}")

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr("hl_mem.http_utils.time.sleep", lambda _delay: None)
    provider = HLMemProvider(timeout=2.0)
    provider.queue_prefetch("query")
    provider._prefetch_cache.drain(2.0)
    assert provider.prefetch("query") == "cached memory"
    assert provider.flush_delivery_receipts() == 1

    error = httpx.ReadTimeout("slow")
    for index in range(5):
        query = f"timeout-{index}"
        provider.queue_prefetch(query)
        provider._prefetch_cache.drain(2.0)
        assert provider.prefetch(query) == ""

    request_count = calls
    provider.queue_prefetch("circuit-open")
    provider._prefetch_cache.drain(2.0)
    assert provider.prefetch("circuit-open") == ""
    assert calls == request_count

    provider._circuit_open_until = 0
    error = None
    provider.queue_prefetch("recovered")
    provider._prefetch_cache.drain(2.0)
    assert provider.prefetch("recovered") == "cached memory"


def test_prefetch_forwards_parameters_isolates_keys_and_caches_no_receipts(
    monkeypatch,
) -> None:
    requests = []
    provider = HLMemProvider(timeout=2.0)

    def post(path, payload, *, headers=None):
        requests.append((path, payload, headers))
        return JsonResponse({"retrieval_bundle": _bundle_payload(f"query-limit-{payload['limit']}")})

    monkeypatch.setattr(provider._client, "post", post)

    common = {
        "intent": "preference",
        "as_of": "2026-07-01T00:00:00+00:00",
        "session_id": "session-1",
        "known_as_of": "2026-07-02T00:00:00+00:00",
        "namespace": "project-a",
        "token_budget": 41,
    }
    provider.queue_prefetch("same query", limit=3, **common)
    provider.queue_prefetch("same query", limit=4, **common)
    provider._prefetch_cache.drain(2.0)

    assert len(requests) == 2
    assert {request[0] for request in requests} == {"/v1/internal/retrieval-bundles"}
    assert all(request[2] is None for request in requests)
    payloads = sorted((request[1] for request in requests), key=lambda item: item["limit"])
    assert payloads == [
        {
            "query": "same query",
            "session_id": "session-1",
            "limit": 3,
            "intent": "preference",
            "as_of": "2026-07-01T00:00:00+00:00",
            "known_as_of": "2026-07-02T00:00:00+00:00",
            "namespace": "project-a",
            "token_budget": 41,
            "context_mode": "packed",
        },
        {
            "query": "same query",
            "session_id": "session-1",
            "limit": 4,
            "intent": "preference",
            "as_of": "2026-07-01T00:00:00+00:00",
            "known_as_of": "2026-07-02T00:00:00+00:00",
            "namespace": "project-a",
            "token_budget": 41,
            "context_mode": "packed",
        },
    ]

    first = provider._prefetch_cache.get(
        "session-1", "same query", limit=3, **{k: v for k, v in common.items() if k != "session_id"}
    )
    second = provider._prefetch_cache.get(
        "session-1", "same query", limit=4, **{k: v for k, v in common.items() if k != "session_id"}
    )
    assert first is not None
    assert second is not None
    assert first.query_id == "query-limit-3"
    assert second.query_id == "query-limit-4"
    assert all("feedback_id" not in item for item in retrieval_bundle_to_dict(first)["items"])
    assert "feedback_id" not in retrieval_bundle_to_dict(first)


def test_each_delivery_materializes_fresh_receipts_with_stable_text(
    monkeypatch,
) -> None:
    provider = HLMemProvider(timeout=2.0)
    materialized = []
    injected = []
    materialize_count = 0

    monkeypatch.setattr(
        provider._client,
        "recall_bundle",
        lambda _payload: JsonResponse(
            {"retrieval_bundle": _bundle_payload("stable-query", ("first memory", "second memory"))}
        ),
    )

    def materialize(bundle):
        nonlocal materialize_count
        materialize_count += 1
        materialized.append(bundle)
        return JsonResponse({"context_packet": _packet_payload(bundle, f"delivery-{materialize_count}")})

    def mark_injected(feedback_ids):
        injected.append(feedback_ids)
        return JsonResponse({"updated": len(feedback_ids)})

    monkeypatch.setattr(provider._client, "materialize_context_packet", materialize)
    monkeypatch.setattr(provider._client, "mark_feedback_injected", mark_injected)

    provider.queue_prefetch("query", session_id="session-1")
    provider._prefetch_cache.drain(2.0)

    first_text = provider.prefetch("query", session_id="session-1", turn_id="turn-1")
    second_text = provider.prefetched("query", session_id="session-1", turn_id="turn-2")

    assert first_text == second_text == "first memory\nsecond memory"
    assert len(materialized) == 2
    assert materialized[0] == materialized[1]
    assert all("feedback_id" not in item for item in materialized[0]["items"])
    assert injected == []
    assert provider.health()["delivery"]["pending_injections"] == 2
    assert provider.flush_delivery_receipts() == 2
    assert injected == [
        ["delivery-1-1", "delivery-1-2"],
        ["delivery-2-1", "delivery-2-2"],
    ]
    receipts = provider.delivery_receipts
    assert [receipt.query_id for receipt in receipts] == [
        "stable-query",
        "stable-query",
    ]
    assert [receipt.turn for receipt in receipts] == ["turn-1", "turn-2"]
    assert receipts[0].feedback_ids != receipts[1].feedback_ids


def test_injected_failure_does_not_block_delivery_and_flush_is_bounded(
    monkeypatch,
) -> None:
    provider = HLMemProvider(timeout=2.0)
    mark_attempts = 0

    monkeypatch.setattr(
        provider._client,
        "recall_bundle",
        lambda _payload: JsonResponse({"retrieval_bundle": _bundle_payload("query-1")}),
    )
    monkeypatch.setattr(
        provider._client,
        "materialize_context_packet",
        lambda bundle: JsonResponse({"context_packet": _packet_payload(bundle)}),
    )

    def fail_mark(_feedback_ids):
        nonlocal mark_attempts
        mark_attempts += 1
        raise httpx.ConnectError("injected endpoint unavailable")

    monkeypatch.setattr(provider._client, "mark_feedback_injected", fail_mark)

    provider.queue_prefetch("query", session_id="session-1")
    provider._prefetch_cache.drain(2.0)

    assert provider.prefetch("query", session_id="session-1") == "cached memory"
    before_flush = provider.health()["delivery"]
    assert before_flush["injection_failures"] == 0
    assert before_flush["pending_injections"] == 1

    for _ in range(3):
        assert provider.flush_delivery_receipts() == 0
    after_flush = provider.health()["delivery"]
    assert mark_attempts == 3
    assert after_flush["injection_failures"] == 3
    assert after_flush["injection_retries"] == 2
    assert after_flush["injection_abandoned"] == 1
    assert after_flush["pending_injections"] == 0


def test_concurrent_delivery_and_retry_requeue_stays_bounded(monkeypatch) -> None:
    provider = HLMemProvider(timeout=2.0)
    for index in range(MAX_DELIVERY_RECEIPTS):
        provider._record_delivery(
            session_id="session-1",
            turn_id=index,
            query_id=f"query-{index}",
            feedback_ids=(f"feedback-{index}",),
        )

    def fail_after_concurrent_delivery(_receipt):
        provider._record_delivery(
            session_id="session-2",
            turn_id="concurrent",
            query_id="query-concurrent",
            feedback_ids=("feedback-concurrent",),
        )
        return False

    monkeypatch.setattr(
        provider,
        "_try_mark_injected",
        fail_after_concurrent_delivery,
    )

    assert provider.flush_delivery_receipts(max_items=1) == 0
    health = provider.health()["delivery"]
    assert health["pending_injections"] == MAX_DELIVERY_RECEIPTS
    assert health["retained_receipts"] == MAX_DELIVERY_RECEIPTS
    assert health["injection_abandoned"] == 1


def test_valid_cached_bundle_renders_when_materialization_fails(
    monkeypatch,
) -> None:
    provider = HLMemProvider(timeout=2.0)
    mark_calls = []

    monkeypatch.setattr(
        provider._client,
        "recall_bundle",
        lambda _payload: JsonResponse(
            {"retrieval_bundle": _bundle_payload("cached-query", ("cached first", "cached second"))}
        ),
    )

    def fail_materialize(_bundle):
        raise httpx.ConnectError("materialization unavailable")

    monkeypatch.setattr(provider._client, "materialize_context_packet", fail_materialize)
    monkeypatch.setattr(
        provider._client,
        "mark_feedback_injected",
        lambda feedback_ids: mark_calls.append(feedback_ids),
    )

    provider.queue_prefetch("query", session_id="session-1")
    provider._prefetch_cache.drain(2.0)

    assert provider.prefetch("query", session_id="session-1") == "cached first\ncached second"
    delivery_health = provider.health()["delivery"]
    assert delivery_health["materialization_failures"] == 1
    assert delivery_health["deliveries"] == 1
    assert delivery_health["pending_injections"] == 0
    assert mark_calls == []
    assert provider.delivery_receipts[-1].feedback_ids == ()


def test_unknown_materialized_schema_major_fails_open_to_empty_context(
    monkeypatch,
) -> None:
    provider = HLMemProvider(timeout=2.0)
    monkeypatch.setattr(
        provider._client,
        "recall_bundle",
        lambda _payload: JsonResponse({"retrieval_bundle": _bundle_payload("cached-query")}),
    )
    monkeypatch.setattr(
        provider._client,
        "materialize_context_packet",
        lambda _bundle: JsonResponse({"context_packet": {"schema_major": 2, "items": []}}),
    )

    provider.queue_prefetch("query", session_id="session-1")
    provider._prefetch_cache.drain(2.0)

    assert provider.prefetch("query", session_id="session-1") == ""
    delivery_health = provider.health()["delivery"]
    assert delivery_health["schema_failures"] == 1
    assert delivery_health["deliveries"] == 0
    assert provider.delivery_receipts == ()


def test_different_prefetch_keys_run_concurrently_without_dropping_tasks(
    monkeypatch,
) -> None:
    provider = HLMemProvider(timeout=3.0)
    rendezvous = threading.Barrier(2, timeout=2.0)
    queries = []

    def recall_bundle(payload):
        queries.append(payload["query"])
        rendezvous.wait()
        return JsonResponse(
            {"retrieval_bundle": _bundle_payload(f"query-{payload['query']}", (f"memory {payload['query']}",))}
        )

    monkeypatch.setattr(provider._client, "recall_bundle", recall_bundle)

    provider.queue_prefetch("one", session_id="session-1")
    provider.queue_prefetch("two", session_id="session-2")
    provider._prefetch_cache.drain(3.0)

    budget = provider.settings.packed_context_token_budget
    first = provider._prefetch_cache.get("session-1", "one", token_budget=budget)
    second = provider._prefetch_cache.get("session-2", "two", token_budget=budget)
    assert sorted(queries) == ["one", "two"]
    assert first is not None
    assert second is not None
    assert first.items[0].text == "memory one"
    assert second.items[0].text == "memory two"
    assert provider.health()["prefetch"]["completed"] == 2


def test_prefetch_state_is_pending_completed_then_expired_and_same_key_dedupes(
    monkeypatch,
) -> None:
    provider = HLMemProvider(timeout=2.0)
    started = threading.Event()
    release = threading.Event()
    calls = 0
    now = [100.0]
    provider._prefetch_cache._clock = lambda: now[0]

    def recall_bundle(_payload):
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(1.0)
        return JsonResponse({"retrieval_bundle": _bundle_payload("state-query")})

    monkeypatch.setattr(provider._client, "recall_bundle", recall_bundle)
    budget = provider.settings.packed_context_token_budget

    provider.queue_prefetch("query", session_id="session-1")
    assert started.wait(1.0)
    provider.queue_prefetch("query", session_id="session-1")
    pending = provider._prefetch_cache.inspect(
        "session-1",
        "query",
        token_budget=budget,
    )
    assert pending is not None
    assert pending.status == "pending"
    assert calls == 1

    release.set()
    provider._prefetch_cache.drain(2.0)
    completed = provider._prefetch_cache.inspect(
        "session-1",
        "query",
        token_budget=budget,
    )
    assert completed is not None
    assert completed.status == "completed"

    now[0] += provider._prefetch_cache.ttl_seconds
    expired = provider._prefetch_cache.inspect(
        "session-1",
        "query",
        token_budget=budget,
    )
    assert expired is not None
    assert expired.status == "expired"
    assert expired.bundle is None


def test_prefetch_overload_is_explicit_and_cache_stays_bounded(monkeypatch) -> None:
    provider = HLMemProvider(timeout=2.0)
    cache = PrefetchCache(
        provider._client,
        ttl_seconds=60.0,
        max_workers=1,
        max_entries=2,
    )
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def recall_bundle(payload):
        nonlocal calls
        calls += 1
        if payload["query"] == "stable":
            return JsonResponse({"retrieval_bundle": _bundle_payload("stable-query")})
        started.set()
        assert release.wait(2.0)
        return JsonResponse({"retrieval_bundle": _bundle_payload("bounded-query")})

    monkeypatch.setattr(provider._client, "recall_bundle", recall_bundle)

    cache.queue("stable", "session-0")
    cache.drain(1.0)
    cache.queue("one", "session-1")
    assert started.wait(1.0)
    cache.queue("two", "session-2")
    cache.queue("three", "session-3")

    rejected = cache.inspect("session-3", "three")
    health = cache.health()
    assert rejected is not None
    assert rejected.status == "expired"
    assert rejected.error_type == "PrefetchOverloadedError"
    assert health["pending"] == 1
    assert health["completed"] == 1
    assert health["expired"] == 2
    assert health["cached_entries"] == 2
    assert health["rejection_entries"] == 2
    assert health["queued_tasks"] == 1
    assert health["overload_rejections"] == 2
    assert cache.get("session-0", "stable") is not None
    assert calls == 2

    release.set()
    cache.shutdown(2.0)
    assert cache.inspect("session-1", "one").status == "completed"


def test_prefetch_invalidation_cancels_queued_session_work(monkeypatch) -> None:
    provider = HLMemProvider(timeout=2.0)
    cache = PrefetchCache(
        provider._client,
        ttl_seconds=60.0,
        max_workers=1,
        max_entries=6,
        max_pending=3,
    )
    started = threading.Event()
    release = threading.Event()
    queries = []

    def recall_bundle(payload):
        queries.append(payload["query"])
        if payload["query"] == "running":
            started.set()
            assert release.wait(2.0)
        return JsonResponse({"retrieval_bundle": _bundle_payload(f"query-{payload['query']}")})

    monkeypatch.setattr(provider._client, "recall_bundle", recall_bundle)

    cache.queue("running", "ended-session")
    assert started.wait(1.0)
    cache.queue("queued-one", "ended-session")
    cache.queue("queued-two", "ended-session")
    assert cache.health()["queued_tasks"] == 3

    cache.invalidate_session("ended-session")
    cache.queue("live", "live-session")
    release.set()
    cache.drain(2.0)

    assert queries == ["running", "live"]
    assert cache.inspect("ended-session", "running") is None
    assert cache.get("live-session", "live") is not None
    cache.shutdown(0.0)


def test_prefetch_shutdown_rejects_new_work_explicitly(monkeypatch) -> None:
    provider = HLMemProvider(timeout=2.0)
    cache = PrefetchCache(provider._client, ttl_seconds=60.0)
    calls = 0

    def recall_bundle(_payload):
        nonlocal calls
        calls += 1
        return JsonResponse({"retrieval_bundle": _bundle_payload("never")})

    monkeypatch.setattr(provider._client, "recall_bundle", recall_bundle)

    cache.shutdown(0.0)
    cache.queue("after-shutdown", "session-1")

    entry = cache.inspect("session-1", "after-shutdown")
    health = cache.health()
    assert entry is not None
    assert entry.status == "expired"
    assert entry.error_type == "PrefetchClosedError"
    assert health["closed"] is True
    assert health["closed_rejections"] == 1
    assert health["queued_tasks"] == 0
    assert calls == 0


def test_sync_turn_extracts_episode_and_tool_traces(monkeypatch) -> None:
    AsyncClient.calls = 0
    AsyncClient.requests = []
    AsyncClient.error = None
    monkeypatch.setattr(httpx, "AsyncClient", AsyncClient)
    monkeypatch.setattr(httpx, "post", lambda *_args, **_kwargs: Response())
    provider = HLMemProvider(timeout=2.0)

    messages = [
        {"role": "user", "content": "修复项目并部署", "session_id": "session-1"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
                },
                {
                    "id": "call-2",
                    "function": {"name": "patch", "arguments": '{"path":"a.py"}'},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "file contents"},
        {"role": "tool", "tool_call_id": "call-2", "content": "patched"},
        {"role": "assistant", "content": "修复完成"},
    ]
    provider.sync_turn(
        "修复项目并部署",
        "修复完成",
        session_id="session-1",
        namespace="project-a",
        messages=messages,
    )

    episode_requests = [(url, payload) for url, payload in AsyncClient.requests if "/v1/episodes" in url]
    assert episode_requests[0][1] == {
        "goal": "修复项目并部署",
        "namespace": "project-a",
        "session_id": "session-1",
        "task_type": "coding",
    }
    traces = [payload for url, payload in episode_requests if url.endswith("/traces")]
    assert [trace["action"] for trace in traces] == ["read_file", "patch"]
    assert [trace["observation"] for trace in traces] == ["[success] file contents", "[success] patched"]
    assert episode_requests[-1][0].endswith("/v1/episodes/episode-1")
    assert episode_requests[-1][1]["status"] == "success"
    assert episode_requests[-1][1]["reward"] == 0.8


def test_sync_turn_uses_current_content_and_only_traces_after_last_user(monkeypatch) -> None:
    AsyncClient.calls = 0
    AsyncClient.requests = []
    AsyncClient.error = None
    monkeypatch.setattr(httpx, "AsyncClient", AsyncClient)
    monkeypatch.setattr(httpx, "post", lambda *_args, **_kwargs: Response())
    provider = HLMemProvider(timeout=2.0)
    messages = [
        {"role": "user", "content": "S" * 7_000},
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "old-1", "function": {"name": "old_read"}},
                {"id": "old-2", "function": {"name": "old_patch"}},
            ],
        },
        {"role": "tool", "tool_call_id": "old-1", "content": "old result"},
        {"role": "tool", "tool_call_id": "old-2", "content": "old result"},
        {"role": "user", "content": "消息列表里的当前任务"},
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "new-1", "function": {"name": "read_file"}},
                {"id": "new-2", "function": {"name": "patch"}},
            ],
        },
        {"role": "tool", "tool_call_id": "new-1", "content": "new file"},
        {"role": "tool", "tool_call_id": "new-2", "content": "new patch"},
        {"role": "assistant", "content": "完成"},
    ]

    provider.sync_turn("本轮 content 才是 goal", "完成", session_id="session-1", messages=messages)

    episode_requests = [(url, payload) for url, payload in AsyncClient.requests if "/v1/episodes" in url]
    assert episode_requests[0][1]["goal"] == "本轮 content 才是 goal"
    traces = [payload for url, payload in episode_requests if url.endswith("/traces")]
    assert [trace["action"] for trace in traces] == ["read_file", "patch"]
    assert [trace["observation"] for trace in traces] == ["[success] new file", "[success] new patch"]


def test_sync_turn_truncates_long_episode_goal_and_falls_back_for_blank_content(monkeypatch) -> None:
    AsyncClient.calls = 0
    AsyncClient.requests = []
    AsyncClient.error = None
    monkeypatch.setattr(httpx, "AsyncClient", AsyncClient)
    monkeypatch.setattr(httpx, "post", lambda *_args, **_kwargs: Response())
    provider = HLMemProvider(timeout=2.0)
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "call-1", "function": {"name": "read_file"}},
                {"id": "call-2", "function": {"name": "patch"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "file"},
        {"role": "tool", "tool_call_id": "call-2", "content": "patched"},
    ]

    provider.sync_turn("g" * 6_000, "done", messages=messages)
    provider.sync_turn("   ", "done", messages=messages)

    goals = [payload["goal"] for url, payload in AsyncClient.requests if url.endswith("/v1/episodes")]
    assert goals == ["g" * 5_000, "Complete tool-assisted task"]


def test_episode_422_diagnostic_removes_rejected_goal_content() -> None:
    response = httpx.Response(
        422,
        json={
            "detail": [
                {
                    "type": "string_too_long",
                    "loc": ["body", "goal"],
                    "msg": "String should have at most 5000 characters",
                    "input": "private compaction summary",
                }
            ]
        },
    )

    diagnostic = _validation_response_body(response)

    assert "string_too_long" in diagnostic
    assert "private compaction summary" not in diagnostic


def test_sync_turn_summarizes_long_observation(monkeypatch) -> None:
    AsyncClient.calls = 0
    AsyncClient.requests = []
    AsyncClient.error = None
    monkeypatch.setattr(httpx, "AsyncClient", AsyncClient)
    monkeypatch.setattr(httpx, "post", lambda *_args, **_kwargs: Response())
    provider = HLMemProvider(timeout=2.0)
    long_action = "a" * 10_001
    long_observation = "o" * 60_000
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "call-1", "function": {"name": long_action}},
                {"id": "call-2", "function": {"name": "patch"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": long_observation},
        {"role": "tool", "tool_call_id": "call-2", "content": "patched"},
    ]

    provider.sync_turn("task", "done", messages=messages)

    traces = [payload for url, payload in AsyncClient.requests if url.endswith("/traces")]
    assert len(traces[0]["action"]) == 10_000
    assert traces[0]["observation"] == f"[success] {'o' * 500}"
    assert len(traces[0]["observation"]) == 510


def test_sync_turn_marks_error_observation(monkeypatch) -> None:
    AsyncClient.calls = 0
    AsyncClient.requests = []
    AsyncClient.error = None
    monkeypatch.setattr(httpx, "AsyncClient", AsyncClient)
    monkeypatch.setattr(httpx, "post", lambda *_args, **_kwargs: Response())
    provider = HLMemProvider(timeout=2.0)
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "call-1", "function": {"name": "test"}},
                {"id": "call-2", "function": {"name": "patch"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "FAILED: command returned exit code 1"},
        {"role": "tool", "tool_call_id": "call-2", "content": "patched"},
    ]

    provider.sync_turn("task", "done", messages=messages)

    traces = [payload for url, payload in AsyncClient.requests if url.endswith("/traces")]
    assert traces[0]["observation"] == "[error] FAILED: command returned exit code 1"


def test_sync_turn_preserves_short_observation_content(monkeypatch) -> None:
    AsyncClient.calls = 0
    AsyncClient.requests = []
    AsyncClient.error = None
    monkeypatch.setattr(httpx, "AsyncClient", AsyncClient)
    monkeypatch.setattr(httpx, "post", lambda *_args, **_kwargs: Response())
    provider = HLMemProvider(timeout=2.0)
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "call-1", "function": {"name": "test"}},
                {"id": "call-2", "function": {"name": "patch"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "3 passed"},
        {"role": "tool", "tool_call_id": "call-2", "content": "patched"},
    ]

    provider.sync_turn("task", "done", messages=messages)

    traces = [payload for url, payload in AsyncClient.requests if url.endswith("/traces")]
    assert traces[0]["observation"] == "[success] 3 passed"


def test_sync_turn_episode_failure_does_not_fail_event_sync(monkeypatch) -> None:
    class EpisodeFailingClient(AsyncClient):
        episode_attempts = 0

        async def post(self, url, **kwargs):
            if url.endswith("/v1/episodes"):
                type(self).episode_attempts += 1
                raise httpx.ConnectError("episode unavailable")
            return await super().post(url, **kwargs)

    event_requests = []

    def post(url, **kwargs):
        event_requests.append((url, kwargs["json"]))
        return Response()

    EpisodeFailingClient.calls = 0
    EpisodeFailingClient.requests = []
    EpisodeFailingClient.error = None
    EpisodeFailingClient.episode_attempts = 0
    monkeypatch.setattr(httpx, "AsyncClient", EpisodeFailingClient)
    monkeypatch.setattr(httpx, "post", post)
    provider = HLMemProvider(timeout=2.0)
    messages = [
        {
            "role": "assistant",
            "tool_calls": [{"id": "1", "function": {"name": "web_search"}}],
        },
        {
            "role": "assistant",
            "tool_calls": [{"id": "2", "function": {"name": "web_search"}}],
        },
    ]

    provider.sync_turn("user request", "assistant response", messages=messages)

    assert provider._failure_count == 0
    assert len(event_requests) == 1
    assert event_requests[0][0].endswith("/v1/events/batch")
    assert [payload["actor_type"] for payload in event_requests[0][1]["events"]] == [
        "user",
        "assistant",
    ]
    assert EpisodeFailingClient.episode_attempts == 1
