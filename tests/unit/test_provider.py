import httpx
import pytest

from hl_mem.adapters.hermes.provider import HLMemProvider


class Response:
    def raise_for_status(self): pass
    def json(self): return {"results": [{"id": "one"}]}


class AsyncClient:
    calls = 0
    error = None

    def __init__(self, **kwargs):
        assert kwargs["timeout"] == 2.0

    async def __aenter__(self): return self
    async def __aexit__(self, *_args): return None

    async def post(self, *_args, **_kwargs):
        type(self).calls += 1
        if self.error:
            raise self.error
        return Response()


@pytest.mark.asyncio
async def test_prefetch_success_timeout_and_circuit(monkeypatch) -> None:
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
