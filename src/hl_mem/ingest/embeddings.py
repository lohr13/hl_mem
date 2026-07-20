from __future__ import annotations

import hashlib
import math
import struct
from typing import Iterable

import httpx


def pack_vector(values: Iterable[float]) -> bytes:
    values = list(values)
    return struct.pack(f"<{len(values)}f", *values)


def unpack_vector(blob: bytes) -> tuple[float, ...]:
    if len(blob) % 4:
        raise ValueError("embedding BLOB length must be divisible by four")
    return struct.unpack(f"<{len(blob) // 4}f", blob)


def cosine_similarity(blob_a: bytes, blob_b: bytes) -> float:
    a, b = unpack_vector(blob_a), unpack_vector(blob_b)
    if len(a) != len(b):
        raise ValueError("embedding dimensions differ")
    denominator = math.sqrt(sum(x * x for x in a) * sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / denominator if denominator else 0.0


class Embedder:
    """OpenAI-compatible embedding client using HTTP only."""

    MAX_BATCH_SIZE = 10

    def __init__(self, api_key: str, base_url: str, model: str, dim: int = 2048) -> None:
        self.api_key, self.base_url, self.model, self.dim = api_key, base_url.rstrip("/"), model, dim

    def embed(self, texts: list[str]) -> list[bytes]:
        return self.embed_batch(texts)

    def embed_batch(self, texts: list[str]) -> list[bytes]:
        result: list[bytes] = []
        for start in range(0, len(texts), self.MAX_BATCH_SIZE):
            result.extend(self._request(texts[start:start + self.MAX_BATCH_SIZE]))
        return result

    def embed_one(self, text: str) -> bytes:
        return self.embed_batch([text])[0]

    def _request(self, texts: list[str]) -> list[bytes]:
        response = httpx.post(
            f"{self.base_url}/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "input": texts, "dimensions": self.dim},
            timeout=30.0,
        )
        response.raise_for_status()
        data = sorted(response.json()["data"], key=lambda item: item.get("index", 0))
        if len(data) != len(texts):
            raise ValueError("embedding response count does not match input count")
        blobs = [pack_vector(item["embedding"]) for item in data]
        if any(len(blob) != self.dim * 4 for blob in blobs):
            raise ValueError("embedding response dimension does not match configured dimension")
        return blobs


class FakeEmbedder:
    """Deterministic, local BLOB embedder suitable for all offline tests."""

    MAX_BATCH_SIZE = 10

    def __init__(self, dim: int = 2048) -> None:
        self.dim = dim

    def embed(self, texts: list[str]) -> list[bytes]:
        return self.embed_batch(texts)

    def embed_batch(self, texts: list[str]) -> list[bytes]:
        return [self.embed_one(text) for text in texts]

    def embed_one(self, text: str) -> bytes:
        seed = hashlib.sha256(text.casefold().encode("utf-8")).digest()
        values = [((seed[index % len(seed)] / 127.5) - 1.0) for index in range(self.dim)]
        return pack_vector(values)
