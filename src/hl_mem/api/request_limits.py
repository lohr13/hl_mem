"""Transport-level ASGI request body size enforcement."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from enum import Enum

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class _ContentLengthState(Enum):
    INVALID = "invalid"
    TOO_LARGE = "too_large"


def parse_content_length(headers: Iterable[tuple[bytes, bytes]]) -> int | None | _ContentLengthState:
    """Parse all Content-Length fields, rejecting malformed or unequal values."""
    values = [value for name, value in headers if name.lower() == b"content-length"]
    if not values:
        return None
    if any(not value or not value.isdigit() for value in values):
        return _ContentLengthState.INVALID
    normalized = [value.lstrip(b"0") or b"0" for value in values]
    if any(value != normalized[0] for value in normalized[1:]):
        return _ContentLengthState.INVALID
    try:
        return int(normalized[0])
    except ValueError:
        return _ContentLengthState.TOO_LARGE


async def send_plain_response(send: Send, status: int, body: bytes) -> None:
    """Send a complete plain-text ASGI response."""
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-length", str(len(body)).encode("ascii")),
                (b"content-type", b"text/plain; charset=utf-8"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def replay(messages: list[Message], receive: Receive) -> Receive:
    """Replay buffered messages, then resume reads from the original receive."""
    buffered = deque(messages)

    async def receive_replayed() -> Message:
        if buffered:
            return buffered.popleft()
        return await receive()

    return receive_replayed


class RequestSizeLimitMiddleware:
    """Reject HTTP bodies exceeding a byte limit and replay accepted messages."""

    def __init__(self, app: ASGIApp, max_request_body: int) -> None:
        if max_request_body < 0:
            raise ValueError("max_request_body must be non-negative")
        self.app = app
        self.max_request_body = max_request_body

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = parse_content_length(scope.get("headers", []))
        if declared is _ContentLengthState.INVALID:
            await send_plain_response(send, 400, b"Invalid Content-Length")
            return
        if declared is _ContentLengthState.TOO_LARGE or declared is not None and declared > self.max_request_body:
            await send_plain_response(send, 413, b"Request body too large")
            return

        messages: list[Message] = []
        retained = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                messages.append(message)
                break
            if message["type"] != "http.request":
                continue

            body = message.get("body", b"")
            if len(body) > self.max_request_body - retained:
                await send_plain_response(send, 413, b"Request body too large")
                return
            retained += len(body)
            messages.append(message)
            if not message.get("more_body", False):
                break

        await self.app(scope, replay(messages, receive), send)
