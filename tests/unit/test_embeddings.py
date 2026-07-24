import struct

import httpx
import pytest

from hl_mem.ingest.embedder import Embedder, FakeEmbedder, pack_vector, unpack_vector
from hl_mem.core.vector import cosine_similarity


def test_fake_dimension_and_vector_round_trip() -> None:
    blob = FakeEmbedder(8).embed_one("中文")
    assert len(blob) == 32
    assert unpack_vector(pack_vector([1.5, -2.0])) == pytest.approx((1.5, -2.0))


def test_cosine_similarity() -> None:
    x, y = struct.pack("<2f", 1, 0), struct.pack("<2f", 0, 1)
    assert cosine_similarity(x, x) == pytest.approx(1.0)
    assert cosine_similarity(x, y) == pytest.approx(0.0)


def test_real_embedder_chunks_at_ten(monkeypatch) -> None:
    batches = []

    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {"data": [{"index": i, "embedding": [1.0, 0.0]} for i in range(len(batches[-1]))]}

    def post(*args, **kwargs):
        batches.append(kwargs["json"]["input"])
        return Response()

    monkeypatch.setattr(httpx, "post", post)
    assert len(Embedder("key", "https://example.test", "model", 2).embed_batch([str(i) for i in range(11)])) == 11
    assert [len(batch) for batch in batches] == [10, 1]
