"""Governed remote and deterministic test embedding components."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from typing import Any, Literal, cast

from hl_mem.core.vector import pack_vector
from hl_mem.observability.usage import UsageAmount
from hl_mem.plugins.contracts import (
    EmbeddingInvocation,
    EmbeddingProviderAdapter,
    EmbeddingResult,
    ProviderEndpoint,
    ProviderFactoryContext,
    ProviderRequest,
    ProviderResponse,
)
from hl_mem.plugins.proxies import GovernedProviderCall


class DashScopeEmbeddingProvider:
    """Translate neutral embedding batches to compatible or native DashScope APIs."""

    name = "dashscope"

    def __init__(self, *, api_mode: Literal["compatible", "native"] = "compatible") -> None:
        if api_mode not in {"compatible", "native"}:
            raise ValueError("embedding api_mode must be 'compatible' or 'native'")
        self.api_mode = api_mode

    def build_request(
        self,
        endpoint: ProviderEndpoint,
        invocation: EmbeddingInvocation,
    ) -> ProviderRequest:
        if invocation.api_mode != self.api_mode:
            raise ValueError("embedding invocation api_mode does not match its Provider adapter")
        dimensions = invocation.dimensions
        if dimensions is None:
            raise ValueError("embedding dimensions are required")
        if self.api_mode == "native":
            url = f"{endpoint.base_url.rstrip('/')}/api/v1/services/embeddings/text-embedding/text-embedding"
            parameters: dict[str, Any] = {"dimension": dimensions}
            if invocation.text_type is not None:
                parameters["text_type"] = invocation.text_type
            payload: dict[str, Any] = {
                "model": endpoint.model,
                "input": {"texts": list(invocation.texts)},
                "parameters": parameters,
            }
        else:
            url = f"{endpoint.base_url.rstrip('/')}/embeddings"
            payload = {
                "model": endpoint.model,
                "input": list(invocation.texts),
                "dimensions": dimensions,
            }
        return ProviderRequest(
            "POST",
            url,
            {"Authorization": f"Bearer {endpoint.api_key}"},
            payload,
            endpoint.timeout_seconds,
            endpoint.connect_timeout_seconds,
        )

    def parse_response(self, response: ProviderResponse) -> EmbeddingResult:
        payload = response.json_body
        if self.api_mode == "native":
            output = payload.get("output")
            if not isinstance(output, Mapping):
                raise ValueError("embedding response output must be an object")
            raw_data = output.get("embeddings")
            index_name = "text_index"
        else:
            raw_data = payload.get("data")
            index_name = "index"
        if not isinstance(raw_data, (list, tuple)):
            raise ValueError("embedding response data must be an array")
        data = self._stable_order(raw_data, index_name)
        vectors: list[tuple[float, ...]] = []
        for item in data:
            if not isinstance(item, Mapping):
                raise ValueError("embedding response item must be an object")
            vector = item.get("embedding")
            if not isinstance(vector, (list, tuple)):
                raise ValueError("embedding response vector must be an array")
            vectors.append(tuple(vector))
        usage = payload.get("usage")
        input_tokens: int | None = None
        if isinstance(usage, Mapping):
            raw_tokens = usage.get("prompt_tokens", usage.get("total_tokens"))
            if raw_tokens is not None:
                input_tokens = int(raw_tokens)
                if input_tokens < 0:
                    raise ValueError("embedding token usage must be non-negative")
        return EmbeddingResult(tuple(vectors), input_tokens=input_tokens)

    @staticmethod
    def _stable_order(raw_data: list[Any] | tuple[Any, ...], index_name: str) -> list[Any]:
        indexes = [item.get(index_name) if isinstance(item, Mapping) else None for item in raw_data]
        if all(index is None for index in indexes):
            return list(raw_data)
        if any(type(index) is not int for index in indexes):
            raise ValueError("embedding response indexes must be integers")
        normalized_indexes = cast(list[int], indexes)
        if sorted(normalized_indexes) != list(range(len(indexes))):
            raise ValueError("embedding response indexes must be unique and contiguous")
        return sorted(raw_data, key=lambda item: int(item[index_name]))


class Embedder:
    """Batching facade that validates vectors and governs each actual request."""

    MAX_BATCH_SIZE = 10

    def __init__(
        self,
        *,
        endpoint: ProviderEndpoint,
        provider: EmbeddingProviderAdapter,
        governed: GovernedProviderCall[list[bytes]],
        dim: int,
        api_mode: Literal["compatible", "native"] = "compatible",
        text_type: Literal["document", "query"] | None = None,
        owned_runtime: object | None = None,
    ) -> None:
        if api_mode not in {"compatible", "native"}:
            raise ValueError("embedding api_mode must be 'compatible' or 'native'")
        if text_type is not None and text_type not in {"document", "query"}:
            raise ValueError("embedding text_type must be 'document' or 'query'")
        if type(dim) is not int or dim < 1:
            raise ValueError("embedding dimension must be a positive integer")
        self.endpoint = endpoint
        self.api_key = endpoint.api_key
        self.base_url = endpoint.base_url.rstrip("/")
        self.model = endpoint.model
        self.dim = dim
        self.api_mode = api_mode
        self.text_type = text_type
        self.timeout = endpoint.timeout_seconds
        self.max_attempts = endpoint.max_attempts
        self.provider = provider
        self._governed = governed
        self._owned_runtime = owned_runtime

    def close(self) -> None:
        runtime = self._owned_runtime
        if runtime is not None:
            close = getattr(runtime, "close", None)
            if callable(close):
                close()
            self._owned_runtime = None

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
        query_text_type: Literal["document", "query"] | None = "query" if self.text_type is not None else None
        for start in range(0, len(texts), self.MAX_BATCH_SIZE):
            result.extend(self._request(texts[start : start + self.MAX_BATCH_SIZE], query_text_type))
        return result

    def _request(
        self,
        texts: list[str],
        text_type: Literal["document", "query"] | None,
    ) -> list[bytes]:
        invocation = EmbeddingInvocation(tuple(texts), self.dim, self.api_mode, text_type)
        estimate = UsageAmount(
            requests=1,
            embedding_items=len(texts),
            unknown_units=frozenset({"input_tokens", "output_tokens"}),
        )
        usage_status = "usage_unknown"

        def parse(response: ProviderResponse) -> tuple[list[bytes], UsageAmount]:
            nonlocal usage_status
            parsed = self.provider.parse_response(response)
            blobs = self._validated_blobs(parsed, expected_count=len(texts))
            if parsed.input_tokens is not None:
                usage_status = "success"
            return (
                blobs,
                UsageAmount(
                    requests=1,
                    input_tokens=parsed.input_tokens or 0,
                    embedding_items=len(texts),
                    unknown_units=frozenset(
                        {"output_tokens"} if parsed.input_tokens is not None else {"input_tokens", "output_tokens"}
                    ),
                ),
            )

        return self._governed.execute_factory(
            lambda: self.provider.build_request(self.endpoint, invocation),
            estimate,
            parse,
            max_attempts=self.max_attempts,
            settlement_status=lambda _value: usage_status,
        )

    def _validated_blobs(self, result: EmbeddingResult, *, expected_count: int) -> list[bytes]:
        if len(result.vectors) != expected_count:
            raise ValueError("embedding response count does not match input count")
        blobs: list[bytes] = []
        for vector in result.vectors:
            if len(vector) != self.dim:
                raise ValueError("embedding response dimension does not match configured dimension")
            if any(
                isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value))
                for value in vector
            ):
                raise ValueError("embedding response values must be finite numeric values")
            blobs.append(pack_vector(float(value) for value in vector))
        return blobs


def make_builtin_embedding_provider(context: ProviderFactoryContext) -> DashScopeEmbeddingProvider:
    if context.key.name != "dashscope":
        raise ValueError(f"unsupported built-in Embedding provider {context.key.name!r}")
    api_mode = str(context.core_options.get("api_mode", "compatible"))
    return DashScopeEmbeddingProvider(api_mode=api_mode)  # type: ignore[arg-type]


class FakeEmbedder:
    """Deterministic local BLOB embedder for explicit fake/test profiles."""

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


__all__ = ["DashScopeEmbeddingProvider", "Embedder", "FakeEmbedder", "make_builtin_embedding_provider"]
