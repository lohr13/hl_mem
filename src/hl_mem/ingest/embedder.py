"""远程与本地测试用文本向量化组件。"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Literal

import httpx

from hl_mem.core.vector import pack_vector
from hl_mem.http_utils import retry_http
from hl_mem.llm.client import classify_provider_error
from hl_mem.monitoring.metrics import DEFAULT_PROVIDER_METRICS, ProviderCall


class Embedder:
    """DashScope compatible/native embedding client using HTTP only."""

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
        *,
        api_mode: Literal["compatible", "native"] = "compatible",
        text_type: Literal["document", "query"] | None = None,
    ) -> None:
        if api_mode not in {"compatible", "native"}:
            raise ValueError("embedding api_mode must be 'compatible' or 'native'")
        if text_type is not None and text_type not in {"document", "query"}:
            raise ValueError("embedding text_type must be 'document' or 'query'")
        self.api_key, self.base_url, self.model, self.dim = (
            api_key,
            base_url.rstrip("/"),
            model,
            dim,
        )
        self.api_mode = api_mode
        self.text_type = text_type
        self.timeout = httpx.Timeout(read_timeout, connect=connect_timeout)
        self.max_attempts = max_attempts
        self._client = client

    def embed(self, texts: list[str]) -> list[bytes]:
        return self.embed_batch(texts)

    def embed_batch(self, texts: list[str]) -> list[bytes]:
        result: list[bytes] = []
        for start in range(0, len(texts), self.MAX_BATCH_SIZE):
            result.extend(self._request(texts[start : start + self.MAX_BATCH_SIZE], self.text_type))
        return result

    def embed_one(self, text: str) -> bytes:
        return self.embed_batch([text])[0]

    def embed_query(self, text: str) -> bytes:
        return self.embed_query_batch([text])[0]

    def embed_query_batch(self, texts: list[str]) -> list[bytes]:
        result: list[bytes] = []
        query_text_type = "query" if self.text_type is not None else None
        for start in range(0, len(texts), self.MAX_BATCH_SIZE):
            result.extend(
                self._request(
                    texts[start : start + self.MAX_BATCH_SIZE],
                    query_text_type,
                )
            )
        return result

    def _request(
        self,
        texts: list[str],
        text_type: Literal["document", "query"] | None,
    ) -> list[bytes]:
        started = time.perf_counter()

        def send_request() -> httpx.Response:
            post = self._client.post if self._client is not None else httpx.post
            payload: dict[str, Any]
            if self.api_mode == "native":
                url = f"{self.base_url}/api/v1/services/embeddings/text-embedding/text-embedding"
                parameters: dict[str, Any] = {"dimension": self.dim}
                if text_type is not None:
                    parameters["text_type"] = text_type
                payload = {
                    "model": self.model,
                    "input": {"texts": texts},
                    "parameters": parameters,
                }
            else:
                url = f"{self.base_url}/embeddings"
                payload = {"model": self.model, "input": texts, "dimensions": self.dim}
            response = post(
                url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response

        try:
            response = retry_http(send_request, max_attempts=self.max_attempts)
        except (
            httpx.ConnectError,
            httpx.TimeoutException,
            httpx.HTTPStatusError,
        ) as error:
            DEFAULT_PROVIDER_METRICS.record(
                ProviderCall(
                    "embedding",
                    "embed",
                    "error",
                    (time.perf_counter() - started) * 1000,
                    error_class=classify_provider_error(error)[0],
                )
            )
            raise RuntimeError(
                f"embedding request failed after {self.max_attempts} attempt(s): {type(error).__name__}: {error}"
            ) from error
        response_payload = response.json()
        if self.api_mode == "native":
            data = sorted(response_payload["output"]["embeddings"], key=lambda item: item.get("text_index", 0))
        else:
            data = sorted(response_payload["data"], key=lambda item: item.get("index", 0))
        if len(data) != len(texts):
            raise ValueError("embedding response count does not match input count")
        blobs = [pack_vector(item["embedding"]) for item in data]
        if any(len(blob) != self.dim * 4 for blob in blobs):
            raise ValueError("embedding response dimension does not match configured dimension")
        DEFAULT_PROVIDER_METRICS.record(
            ProviderCall("embedding", "embed", "success", (time.perf_counter() - started) * 1000)
        )
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
