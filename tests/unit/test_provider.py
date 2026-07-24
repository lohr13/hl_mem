import httpx

from hl_mem.adapters.hermes.provider import HLMemProvider


class Response:
    def raise_for_status(self): pass
    def json(self): return {"results": [{"id": "one"}]}


class AsyncClient:
    calls = 0
    requests = []
    error = None

    def __init__(self, **kwargs):
        assert kwargs["timeout"] == 2.0

    async def __aenter__(self): return self
    async def __aexit__(self, *_args): return None

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
    def json(self): return {"id": "episode-1"}


def test_sync_hooks_post_payloads_and_report_success(monkeypatch) -> None:
    requests = []

    def post(url, **kwargs):
        requests.append((url, kwargs))
        return Response()

    monkeypatch.setattr(httpx, "post", post)
    provider = HLMemProvider("unused.db", "http://memory.test/", timeout=2.0)

    provider.on_memory_write("preference", "喜欢黑咖啡")
    provider.on_pre_compress([{"role": "user", "content": "记住这个偏好"}])

    assert requests == [
        (
            "http://memory.test/v1/memories",
            {
                "json": {
                    "text": "喜欢黑咖啡",
                    "qualifiers": {"key": "preference", "target": "memory"},
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
                },
                "timeout": 2.0,
            },
        ),
    ]
    assert provider._failure_count == 0


def test_sync_hooks_open_circuit_after_repeated_http_failures(monkeypatch) -> None:
    calls = 0

    def post(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("unavailable")

    monkeypatch.setattr(httpx, "post", post)
    provider = HLMemProvider()

    for _ in range(6):
        provider.on_memory_write("key", "value")

    assert calls == 5
    assert provider._circuit_open_until > 0


def test_prefetch_success_timeout_and_circuit(monkeypatch) -> None:
    calls = 0
    error = None

    class PrefetchResponse(Response):
        def json(self):
            return {"results": [{"text": "cached memory"}]}

    def post(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if error:
            raise error
        return PrefetchResponse()

    monkeypatch.setattr(httpx, "post", post)
    provider = HLMemProvider(timeout=2.0)
    provider.queue_prefetch("query")
    provider.shutdown()
    assert provider.prefetch("query") == "cached memory"

    error = httpx.ReadTimeout("slow")
    for index in range(5):
        query = f"timeout-{index}"
        provider.queue_prefetch(query)
        provider.shutdown()
        assert provider.prefetch(query) == ""

    request_count = calls
    provider.queue_prefetch("circuit-open")
    provider.shutdown()
    assert provider.prefetch("circuit-open") == ""
    assert calls == request_count

    provider._circuit_open_until = 0
    error = None
    provider.queue_prefetch("recovered")
    provider.shutdown()
    assert provider.prefetch("recovered") == "cached memory"


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
                {"id": "call-1", "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}},
                {"id": "call-2", "function": {"name": "patch", "arguments": '{"path":"a.py"}'}},
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
        messages=messages,
    )

    episode_requests = [(url, payload) for url, payload in AsyncClient.requests if "/v1/episodes" in url]
    assert episode_requests[0][1] == {
        "goal": "修复项目并部署",
        "session_id": "session-1",
        "task_type": "coding",
    }
    traces = [payload for url, payload in episode_requests if url.endswith("/traces")]
    assert [trace["action"] for trace in traces] == ["read_file", "patch"]
    assert [trace["observation"] for trace in traces] == ["file contents", "patched"]
    assert episode_requests[-1][0].endswith("/v1/episodes/episode-1")
    assert episode_requests[-1][1]["status"] == "success"
    assert episode_requests[-1][1]["reward"] == 0.8


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
        {"role": "assistant", "tool_calls": [{"id": "1", "function": {"name": "web_search"}}]},
        {"role": "assistant", "tool_calls": [{"id": "2", "function": {"name": "web_search"}}]},
    ]

    provider.sync_turn("user request", "assistant response", messages=messages)

    assert provider._failure_count == 0
    assert [payload["actor_type"] for _, payload in event_requests] == ["user", "assistant"]
    assert EpisodeFailingClient.episode_attempts == 1
