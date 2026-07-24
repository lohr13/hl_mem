"""远程与本地测试用文本向量化组件。"""

from __future__ import annotations

import hashlib
import struct
from typing import Iterable

import httpx

from hl_mem.http_utils import retry_http


def pack_vector(values: Iterable[float]) -> bytes:
    values = list(values)
    return struct.pack(f"<{len(values)}f", *values)


def unpack_vector(blob: bytes) -> tuple[float, ...]:
    if len(blob) % 4:
        raise ValueError("embedding BLOB length must be divisible by four")
    return struct.unpack(f"<{len(blob) // 4}f", blob)


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
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key, self.base_url, self.model, self.dim = api_key, base_url.rstrip("/"), model, dim
        self.timeout = httpx.Timeout(read_timeout, connect=connect_timeout)
        self.max_attempts = max_attempts
        self._client = client

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
        def send_request() -> httpx.Response:
            post = self._client.post if self._client is not None else httpx.post
            response = post(
                f"{self.base_url}/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "input": texts, "dimensions": self.dim},
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response

        try:
            response = retry_http(send_request, max_attempts=self.max_attempts)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as error:
            raise RuntimeError(
                f"embedding request failed after {self.max_attempts} attempt(s): {type(error).__name__}: {error}"
            ) from error
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
    model = "fake"

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
