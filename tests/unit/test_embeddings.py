import struct

import pytest

from hl_mem.core.vector import cosine_similarity, pack_vector, unpack_vector
from hl_mem.ingest.embedder import FakeEmbedder


def test_fake_dimension_and_vector_round_trip() -> None:
    blob = FakeEmbedder(8).embed_one("中文")
    assert len(blob) == 32
    assert unpack_vector(pack_vector([1.5, -2.0])) == pytest.approx((1.5, -2.0))


def test_cosine_similarity() -> None:
    x, y = struct.pack("<2f", 1, 0), struct.pack("<2f", 0, 1)
    assert cosine_similarity(x, x) == pytest.approx(1.0)
    assert cosine_similarity(x, y) == pytest.approx(0.0)
