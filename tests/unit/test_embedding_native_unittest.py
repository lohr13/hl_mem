from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hl_mem import protocols as protocols_module  # noqa: E402
from hl_mem.components import create_provider_runtime, make_embedder  # noqa: E402
from hl_mem.config_loader import load_settings  # noqa: E402
from hl_mem.doctor import CheckStatus, _check_embedding  # noqa: E402
from hl_mem.errors import ConfigurationError  # noqa: E402
from hl_mem.settings import Settings  # noqa: E402


def _load_native_settings(root: Path, text_type: str | None = None) -> Settings:
    lines = [
        "schema_version = 1",
        "[database]",
        f'path = {json.dumps(str(root / "memory.db"))}',
        "[embedding]",
        'mode = "real"',
        'base_url = "https://dashscope.aliyuncs.com"',
        'model = "qwen3.7-text-embedding"',
        "dim = 2048",
        'api_mode = "native"',
    ]
    if text_type is not None:
        lines.append(f"text_type = {json.dumps(text_type)}")
    lines.extend(("[recall]", 'query_expansion_mode = "off"'))
    config = root / "hl_mem.toml"
    config.write_text("\n".join(lines), encoding="utf-8")
    return load_settings(
        config,
        root / ".env",
        environ={"EMBEDDING_API_KEY": "key"},
        validate_runtime=False,
    )


class ConfigurationAndRecallTests(unittest.TestCase):
    def test_toml_native_mode_defaults_factory_to_no_text_type(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = _load_native_settings(root)
            runtime = create_provider_runtime(settings)
            try:
                embedder = make_embedder(settings, runtime=runtime)
                self.assertEqual(settings.embedding_api_mode, "native")
                self.assertEqual(embedder.api_mode, "native")
                self.assertIsNone(settings.embedding_text_type)
                self.assertIsNone(embedder.text_type)
            finally:
                runtime.close()

    def test_factory_normalizes_unconfigured_text_type_to_none(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(
                Settings.for_test(),
                database_path=str(Path(directory) / "memory.db"),
                embedder_mode="real",
                embedding_api_key="key",
                embedding_api_mode="native",
            )
            runtime = create_provider_runtime(settings)
            try:
                with patch("hl_mem.components.Embedder") as constructor:
                    make_embedder(settings, runtime=runtime)
                self.assertIsNone(constructor.call_args.kwargs["text_type"])
            finally:
                runtime.close()

    def test_toml_can_explicitly_enable_native_document_role(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = _load_native_settings(root, "document")
            runtime = create_provider_runtime(settings)
            try:
                embedder = make_embedder(settings, runtime=runtime)
                self.assertEqual(settings.embedding_text_type, "document")
                self.assertEqual(embedder.text_type, "document")
            finally:
                runtime.close()

    def test_toml_empty_text_type_normalizes_to_none_in_factory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = _load_native_settings(root, "")
            runtime = create_provider_runtime(settings)
            try:
                embedder = make_embedder(settings, runtime=runtime)
                self.assertEqual(settings.embedding_text_type, "")
                self.assertIsNone(embedder.text_type)
            finally:
                runtime.close()

    def test_settings_reject_invalid_embedding_text_type(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "hl_mem.toml"
            config.write_text('schema_version = 1\n[embedding]\ntext_type = "invalid"\n', encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, r"embedding\.text_type: expected"):
                load_settings(config, root / ".env", environ={}, validate_runtime=False)

    def test_settings_reject_invalid_embedding_api_mode(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "embedding.api_mode"):
            replace(Settings.for_test(), embedding_api_mode="invalid").validate()

    def test_recall_query_helper_uses_query_method_or_legacy_fallback(self) -> None:
        class Native:
            def embed_query(self, text: str) -> bytes:
                return f"query:{text}".encode()

            def embed_one(self, text: str) -> bytes:
                raise AssertionError("native query path must not call embed_one")

        class Legacy:
            def embed_one(self, text: str) -> bytes:
                return f"legacy:{text}".encode()

        self.assertEqual(protocols_module.embed_query(Native(), "hello"), b"query:hello")
        self.assertEqual(protocols_module.embed_query(Legacy(), "hello"), b"legacy:hello")

    def test_query_batch_helper_uses_legacy_batch_fallback(self) -> None:
        class Legacy:
            def __init__(self) -> None:
                self.calls: list[list[str]] = []

            def embed_batch(self, texts: list[str]) -> list[bytes]:
                self.calls.append(list(texts))
                return [text.encode() for text in texts]

            def embed_one(self, text: str) -> bytes:
                raise AssertionError("batch fallback must not degrade to one call per text")

        legacy = Legacy()
        self.assertEqual(protocols_module.embed_queries(legacy, ["a", "b"]), [b"a", b"b"])
        self.assertEqual(legacy.calls, [["a", "b"]])

    def test_doctor_uses_governed_native_request_without_default_text_type(self) -> None:
        calls: list[dict[str, Any]] = []

        def handle(request: httpx.Request) -> httpx.Response:
            calls.append({"url": str(request.url), "json": json.loads(request.content)})
            return httpx.Response(
                200,
                request=request,
                json={
                    "output": {"embeddings": [{"embedding": [1.0, 0.0], "text_index": 0}]},
                    "usage": {"total_tokens": 1},
                },
            )

        with tempfile.TemporaryDirectory() as directory:
            settings = replace(
                Settings.for_test(),
                database_path=str(Path(directory) / "memory.db"),
                embedder_mode="real",
                embedding_api_key="key",
                embedding_base_url="https://dashscope.aliyuncs.com",
                embedding_model="qwen3.7-text-embedding",
                embedding_dim=2,
                embedding_api_mode="native",
            )
            http_client = httpx.Client(transport=httpx.MockTransport(handle))
            runtime = create_provider_runtime(settings, client=http_client)
            try:
                with patch("hl_mem.components.create_provider_runtime", return_value=runtime):
                    result = _check_embedding(settings)
            finally:
                runtime.close()
                http_client.close()

        self.assertEqual(result.status, CheckStatus.OK)
        self.assertEqual(
            calls,
            [
                {
                    "url": "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding",
                    "json": {
                        "model": "qwen3.7-text-embedding",
                        "input": {"texts": ["ping"]},
                        "parameters": {"dimension": 2},
                    },
                }
            ],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
