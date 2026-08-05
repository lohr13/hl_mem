from __future__ import annotations

import struct
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hl_mem import protocols as protocols_module  # noqa: E402
from hl_mem.components import make_embedder  # noqa: E402
from hl_mem.config_loader import load_settings  # noqa: E402
from hl_mem.doctor import CheckStatus, _check_embedding  # noqa: E402
from hl_mem.errors import ConfigurationError  # noqa: E402
from hl_mem.ingest.embedder import Embedder  # noqa: E402
from hl_mem.settings import Settings  # noqa: E402


class _Response:
    status_code = 200

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _RecordingClient:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append({"url": url, **kwargs})
        return _Response(self.responses.pop(0))


class EmbedderApiModeTests(unittest.TestCase):
    def test_compatible_mode_preserves_existing_wire_contract(self) -> None:
        client = _RecordingClient([{"data": [{"index": 0, "embedding": [1.0, 2.0]}], "usage": {"total_tokens": 2}}])
        embedder = Embedder(
            "key",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "text-embedding-v4",
            2,
            client=client,
            api_mode="compatible",
        )

        blob = embedder.embed_one("hello")

        self.assertEqual(struct.unpack("<2f", blob), (1.0, 2.0))
        self.assertEqual(client.calls[0]["url"], "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings")
        self.assertEqual(
            client.calls[0]["json"],
            {"model": "text-embedding-v4", "input": ["hello"], "dimensions": 2},
        )

    def test_native_document_mode_uses_native_payload_and_text_index_order(self) -> None:
        client = _RecordingClient(
            [
                {
                    "output": {
                        "embeddings": [
                            {"embedding": [3.0, 4.0], "text_index": 1},
                            {"embedding": [1.0, 2.0], "text_index": 0},
                        ]
                    },
                    "usage": {"total_tokens": 4},
                }
            ]
        )
        embedder = Embedder(
            "key",
            "https://dashscope.aliyuncs.com",
            "qwen3.7-text-embedding",
            2,
            client=client,
            api_mode="native",
            text_type="document",
        )

        blobs = embedder.embed_batch(["first", "second"])

        self.assertEqual([struct.unpack("<2f", blob) for blob in blobs], [(1.0, 2.0), (3.0, 4.0)])
        self.assertEqual(
            client.calls[0]["url"],
            "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding",
        )
        self.assertEqual(
            client.calls[0]["json"],
            {
                "model": "qwen3.7-text-embedding",
                "input": {"texts": ["first", "second"]},
                "parameters": {"dimension": 2, "text_type": "document"},
            },
        )

    def test_embed_query_overrides_native_role_without_mutating_document_default(self) -> None:
        payload = {
            "output": {"embeddings": [{"embedding": [1.0, 0.0], "text_index": 0}]},
            "usage": {"total_tokens": 1},
        }
        client = _RecordingClient([payload, payload])
        embedder = Embedder(
            "key",
            "https://dashscope.aliyuncs.com",
            "qwen3.7-text-embedding",
            2,
            client=client,
            api_mode="native",
            text_type="document",
        )

        embedder.embed_query("question")
        embedder.embed_one("claim")

        self.assertEqual(client.calls[0]["json"]["parameters"]["text_type"], "query")
        self.assertEqual(client.calls[1]["json"]["parameters"]["text_type"], "document")

    def test_embed_query_batch_preserves_batching_and_query_role(self) -> None:
        client = _RecordingClient(
            [
                {
                    "output": {
                        "embeddings": [
                            {"embedding": [1.0, 0.0], "text_index": 0},
                            {"embedding": [0.0, 1.0], "text_index": 1},
                        ]
                    },
                    "usage": {"total_tokens": 2},
                }
            ]
        )
        embedder = Embedder(
            "key",
            "https://dashscope.aliyuncs.com",
            "qwen3.7-text-embedding",
            2,
            client=client,
            api_mode="native",
        )

        blobs = embedder.embed_query_batch(["first", "second"])

        self.assertEqual(len(blobs), 2)
        self.assertEqual(client.calls[0]["json"]["parameters"]["text_type"], "query")
        self.assertEqual(client.calls[0]["json"]["input"]["texts"], ["first", "second"])

    def test_constructor_rejects_unknown_api_mode_and_text_type(self) -> None:
        with self.assertRaisesRegex(ValueError, "api_mode"):
            Embedder("key", "https://example.test", "model", api_mode="other")
        with self.assertRaisesRegex(ValueError, "text_type"):
            Embedder("key", "https://example.test", "model", text_type="other")


class ConfigurationAndRecallTests(unittest.TestCase):
    def test_toml_loads_native_mode_and_factory_passes_document_role(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "hl_mem.toml"
            config.write_text(
                "\n".join(
                    (
                        "[embedding]",
                        'mode = "real"',
                        'base_url = "https://dashscope.aliyuncs.com"',
                        'model = "qwen3.7-text-embedding"',
                        "dim = 2048",
                        'api_mode = "native"',
                        "[recall]",
                        'query_expansion_mode = "off"',
                    )
                ),
                encoding="utf-8",
            )
            settings = load_settings(config, root / ".env", environ={"EMBEDDING_API_KEY": "key"})

        embedder = make_embedder(settings)
        self.assertEqual(settings.embedding_api_mode, "native")
        self.assertEqual(embedder.api_mode, "native")
        self.assertEqual(embedder.text_type, "document")

    def test_settings_reject_invalid_embedding_api_mode(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "embedding.api_mode"):
            replace(Settings.for_test(), embedding_api_mode="invalid").validate()

    def test_recall_query_helper_uses_query_method_or_legacy_fallback(self) -> None:
        self.assertTrue(hasattr(protocols_module, "embed_query"))
        embed_query = getattr(protocols_module, "embed_query")

        class Native:
            def embed_query(self, text: str) -> bytes:
                return f"query:{text}".encode()

            def embed_one(self, text: str) -> bytes:
                raise AssertionError("native query path must not call embed_one")

        class Legacy:
            def embed_one(self, text: str) -> bytes:
                return f"legacy:{text}".encode()

        self.assertEqual(embed_query(Native(), "hello"), b"query:hello")
        self.assertEqual(embed_query(Legacy(), "hello"), b"legacy:hello")

    def test_query_batch_helper_uses_legacy_batch_fallback(self) -> None:
        self.assertTrue(hasattr(protocols_module, "embed_queries"))
        embed_queries = getattr(protocols_module, "embed_queries")

        class Legacy:
            def __init__(self) -> None:
                self.calls: list[list[str]] = []

            def embed_batch(self, texts: list[str]) -> list[bytes]:
                self.calls.append(list(texts))
                return [text.encode() for text in texts]

            def embed_one(self, text: str) -> bytes:
                raise AssertionError("batch fallback must not degrade to one call per text")

        legacy = Legacy()
        self.assertEqual(embed_queries(legacy, ["a", "b"]), [b"a", b"b"])
        self.assertEqual(legacy.calls, [["a", "b"]])

    def test_doctor_uses_native_document_request(self) -> None:
        response = _Response(
            {
                "output": {"embeddings": [{"embedding": [1.0, 0.0], "text_index": 0}]},
                "usage": {"total_tokens": 1},
            }
        )
        calls: list[dict[str, Any]] = []

        def post(url: str, **kwargs: Any) -> _Response:
            calls.append({"url": url, **kwargs})
            return response

        settings = replace(
            Settings.for_test(),
            embedder_mode="real",
            embedding_api_key="key",
            embedding_base_url="https://dashscope.aliyuncs.com",
            embedding_model="qwen3.7-text-embedding",
            embedding_dim=2,
            embedding_api_mode="native",
        )
        with patch("hl_mem.ingest.embedder.httpx.post", side_effect=post):
            result = _check_embedding(settings)

        self.assertEqual(result.status, CheckStatus.OK)
        self.assertEqual(
            calls[0]["url"],
            "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding",
        )
        self.assertEqual(calls[0]["json"]["parameters"], {"dimension": 2, "text_type": "document"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
