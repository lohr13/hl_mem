from __future__ import annotations

import hashlib
import math
import struct
import time
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

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        dim: int = 2048,
        connect_timeout: float = 5.0,
        read_timeout: float = 30.0,
        max_attempts: int = 3,
    ) -> None:
        self.api_key, self.base_url, self.model, self.dim = api_key, base_url.rstrip("/"), model, dim
        self.timeout = httpx.Timeout(read_timeout, connect=connect_timeout)
        self.max_attempts = max_attempts

    def embed(self, texts: list[str]) -> list[bytes]:
        return self.embed_batch(texts)

    def embed_batch(self, texts: list[str]) -> list[bytes]:
        result: list[bytes] = []
        for start in range(0, len(texts), self.MAX_BATCH_SIZE):
            result.extend(self._request(texts[start : start + self.MAX_BATCH_SIZE]))
        return result

    def embed_one(self, text: str) -> bytes:
        return self.embed_batch([text])[0]

    def _request(self, texts: list[str]) -> list[bytes]:
        response: httpx.Response | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = httpx.post(
                    f"{self.base_url}/embeddings",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"model": self.model, "input": texts, "dimensions": self.dim},
                    timeout=self.timeout,
                )
                response.raise_for_status()
                break
            except (httpx.TimeoutException, httpx.HTTPStatusError) as error:
                retryable = isinstance(error, httpx.TimeoutException) or (
                    error.response is not None
                    and (error.response.status_code == 429 or error.response.status_code >= 500)
                )
                if not retryable or attempt == self.max_attempts:
                    raise RuntimeError(
                        f"embedding request failed after {attempt} attempt(s): {type(error).__name__}: {error}"
                    ) from error
                time.sleep(0.5 * (2 ** (attempt - 1)))
        if response is None:
            raise RuntimeError("embedding request failed without a response")
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
