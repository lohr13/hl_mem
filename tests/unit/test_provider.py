import httpx
import pytest

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


@pytest.mark.asyncio
async def test_prefetch_success_timeout_and_circuit(monkeypatch) -> None:
    AsyncClient.calls = 0
    AsyncClient.requests = []
    AsyncClient.error = None
    monkeypatch.setattr(httpx, "AsyncClient", AsyncClient)
    provider = HLMemProvider(timeout=2.0)
    assert (await provider.prefetch("query"))["results"][0]["id"] == "one"
    AsyncClient.error = httpx.ReadTimeout("slow")
    for _ in range(5):
        assert await provider.prefetch("query") == {"results": [], "error": "timeout"}
    calls = AsyncClient.calls
    assert await provider.prefetch("query") == {"results": [], "error": "circuit_open"}
    assert AsyncClient.calls == calls
    provider._circuit_open_until = 0
    AsyncClient.error = None
    assert (await provider.prefetch("query"))["results"]


@pytest.mark.asyncio
async def test_sync_turn_extracts_episode_and_tool_traces(monkeypatch) -> None:
    AsyncClient.calls = 0
    AsyncClient.requests = []
    AsyncClient.error = None
    monkeypatch.setattr(httpx, "AsyncClient", AsyncClient)
    provider = HLMemProvider(timeout=2.0)

    await provider.sync_turn(
        [
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


@pytest.mark.asyncio
async def test_sync_turn_episode_failure_does_not_fail_event_sync(monkeypatch) -> None:
    class EpisodeFailingClient(AsyncClient):
        async def post(self, url, **kwargs):
            if url.endswith("/v1/episodes"):
                raise httpx.ConnectError("episode unavailable")
            return await super().post(url, **kwargs)

    EpisodeFailingClient.calls = 0
    EpisodeFailingClient.requests = []
    EpisodeFailingClient.error = None
    monkeypatch.setattr(httpx, "AsyncClient", EpisodeFailingClient)
    provider = HLMemProvider(timeout=2.0)
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "1", "function": {"name": "web_search"}}]},
        {"role": "assistant", "tool_calls": [{"id": "2", "function": {"name": "web_search"}}]},
    ]

    await provider.sync_turn(messages)

    assert provider._failure_count == 0
    assert EpisodeFailingClient.calls == 2
