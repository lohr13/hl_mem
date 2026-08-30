"""Raw-ASGI behavioral tests for request body size enforcement."""

from __future__ import annotations

import gc
import weakref
from collections.abc import Iterable
from dataclasses import dataclass

import pytest
from starlette.types import Message, Receive, Scope, Send

from hl_mem.api.request_limits import RequestSizeLimitMiddleware


@dataclass
class Invocation:
    status: int
    body: bytes
    downstream_calls: int
    downstream_body: bytes
    downstream_messages: list[Message]
    source_receive_calls: int


async def invoke_messages(
    messages: Iterable[Message],
    *,
    headers: list[tuple[bytes, bytes]],
    limit: int,
    receive_after_body: bool = False,
) -> Invocation:
    source_messages = iter(messages)
    source_receive_calls = 0
    downstream_calls = 0
    downstream_body = bytearray()
    downstream_messages: list[Message] = []
    response_messages: list[Message] = []

    async def source_receive() -> Message:
        nonlocal source_receive_calls
        source_receive_calls += 1
        return next(source_messages)

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal downstream_calls
        downstream_calls += 1
        while True:
            message = await receive()
            downstream_messages.append(message)
            if message["type"] == "http.disconnect":
                break
            if message["type"] == "http.request":
                downstream_body.extend(message.get("body", b""))
                if not message.get("more_body", False) and not receive_after_body:
                    break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"accepted"})

    async def capture_send(message: Message) -> None:
        response_messages.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "state": {},
    }
    middleware = RequestSizeLimitMiddleware(downstream, max_request_body=limit)
    await middleware(scope, source_receive, capture_send)

    start = next(message for message in response_messages if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"") for message in response_messages if message["type"] == "http.response.body"
    )
    return Invocation(
        status=start["status"],
        body=body,
        downstream_calls=downstream_calls,
        downstream_body=bytes(downstream_body),
        downstream_messages=downstream_messages,
        source_receive_calls=source_receive_calls,
    )


async def invoke(
    chunks: list[bytes],
    *,
    headers: list[tuple[bytes, bytes]],
    limit: int,
) -> Invocation:
    messages: list[Message] = [
        {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
        for index, chunk in enumerate(chunks)
    ]
    if not messages:
        messages.append({"type": "http.request", "body": b"", "more_body": False})
    return await invoke_messages(messages, headers=headers, limit=limit)


@pytest.mark.asyncio
async def test_missing_content_length_cannot_bypass_limit() -> None:
    response = await invoke([b"1234", b"5678", b"9"], headers=[], limit=8)

    assert response.status == 413
    assert response.downstream_calls == 0


@pytest.mark.asyncio
async def test_false_small_content_length_cannot_bypass_limit() -> None:
    response = await invoke([b"12345", b"67890"], headers=[(b"content-length", b"2")], limit=8)

    assert response.status == 413
    assert response.downstream_calls == 0


@pytest.mark.asyncio
async def test_body_at_limit_is_replayed_without_change() -> None:
    response = await invoke([b"123", b"45678"], headers=[], limit=8)

    assert response.status == 200
    assert response.downstream_body == b"12345678"
    assert response.downstream_messages == [
        {"type": "http.request", "body": b"12345678", "more_body": False},
    ]


@pytest.mark.asyncio
async def test_single_frame_bytes_payload_is_reused_without_copy() -> None:
    payload = b"x" * (1024 * 1024)

    response = await invoke([payload], headers=[], limit=len(payload))

    assert response.status == 200
    assert response.downstream_messages[0]["body"] is payload


@pytest.mark.asyncio
async def test_oversized_payload_is_released_before_response_send() -> None:
    class WeakPayload(bytearray):
        pass

    payload: WeakPayload | None = WeakPayload(b"123456789")
    payload_reference = weakref.ref(payload)
    source_message: Message | None = {
        "type": "http.request",
        "body": payload,
        "more_body": False,
    }
    release_states: list[bool] = []
    response_messages: list[Message] = []

    async def source_receive() -> Message:
        nonlocal payload, source_message
        message = source_message
        source_message = None
        payload = None
        assert message is not None
        return message

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        raise AssertionError("oversized payload reached downstream")

    async def capture_send(message: Message) -> None:
        gc.collect()
        release_states.append(payload_reference() is None)
        response_messages.append(message)

    middleware = RequestSizeLimitMiddleware(downstream, max_request_body=8)
    await middleware({"type": "http", "headers": []}, source_receive, capture_send)

    assert next(message["status"] for message in response_messages if message["type"] == "http.response.start") == 413
    assert release_states == [True, True]


@pytest.mark.asyncio
async def test_highly_fragmented_body_has_bounded_replay_metadata() -> None:
    frame_count = 10_000
    messages = (
        {"type": "http.request", "body": b"x", "more_body": index < frame_count - 1} for index in range(frame_count)
    )

    response = await invoke_messages(messages, headers=[], limit=frame_count)

    assert response.status == 200
    assert response.downstream_body == b"x" * frame_count
    assert response.downstream_messages == [
        {"type": "http.request", "body": b"x" * frame_count, "more_body": False},
    ]


@pytest.mark.asyncio
async def test_empty_frame_budget_prevents_unbounded_receive_loop() -> None:
    frame_count = 65_537
    messages = (
        {"type": "http.request", "body": b"", "more_body": index < frame_count - 1} for index in range(frame_count)
    )

    response = await invoke_messages(messages, headers=[], limit=8)

    assert response.status == 413
    assert response.downstream_calls == 0
    assert response.source_receive_calls == frame_count


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [b"abc", b"-1", b"3,4"])
async def test_malformed_content_length_is_bad_request(value: bytes) -> None:
    response = await invoke([b"{}"], headers=[(b"content-length", value)], limit=8)

    assert response.status == 400
    assert response.downstream_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("values", [(b"2", b"3"), (b"20", b"2")])
async def test_unequal_duplicate_content_lengths_are_bad_request(values: tuple[bytes, bytes]) -> None:
    response = await invoke(
        [b"{}"],
        headers=[(b"content-length", values[0]), (b"content-length", values[1])],
        limit=8,
    )

    assert response.status == 400
    assert response.downstream_calls == 0


@pytest.mark.asyncio
async def test_equal_numeric_duplicate_content_lengths_are_accepted() -> None:
    response = await invoke(
        [b"{}"],
        headers=[(b"content-length", b"02"), (b"content-length", b"2")],
        limit=8,
    )

    assert response.status == 200
    assert response.downstream_body == b"{}"


@pytest.mark.asyncio
async def test_equal_duplicate_content_lengths_are_accepted() -> None:
    response = await invoke(
        [b"{}"],
        headers=[(b"content-length", b"2"), (b"content-length", b"2")],
        limit=8,
    )

    assert response.status == 200
    assert response.downstream_body == b"{}"


@pytest.mark.asyncio
async def test_enormous_declared_length_is_rejected_before_reading() -> None:
    response = await invoke([b"ignored"], headers=[(b"content-length", b"9" * 5_000)], limit=8)

    assert response.status == 413
    assert response.downstream_calls == 0
    assert response.source_receive_calls == 0


@pytest.mark.asyncio
async def test_declared_oversize_is_rejected_before_reading() -> None:
    response = await invoke([b"ignored"], headers=[(b"content-length", b"9")], limit=8)

    assert response.status == 413
    assert response.downstream_calls == 0
    assert response.source_receive_calls == 0


@pytest.mark.asyncio
async def test_empty_body_passes() -> None:
    response = await invoke([], headers=[], limit=8)

    assert response.status == 200
    assert response.downstream_body == b""


@pytest.mark.asyncio
async def test_disconnect_is_replayed_without_change() -> None:
    source_messages: list[Message] = [
        {"type": "http.request", "body": b"123", "more_body": True},
        {"type": "http.disconnect"},
    ]

    response = await invoke_messages(source_messages, headers=[], limit=8)

    assert response.status == 200
    assert response.downstream_messages == source_messages


@pytest.mark.asyncio
async def test_receive_after_complete_body_falls_through_to_disconnect() -> None:
    source_messages: list[Message] = [
        {"type": "http.request", "body": b"123", "more_body": False},
        {"type": "http.disconnect"},
    ]

    response = await invoke_messages(source_messages, headers=[], limit=8, receive_after_body=True)

    assert response.status == 200
    assert response.downstream_messages == source_messages
