from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import hl_mem.plugins as public_api

ROOT = Path(__file__).parents[2]


def test_public_provider_api_exports_stable_and_experimental_contracts() -> None:
    stable = {
        "EmbeddingInvocation",
        "EmbeddingProviderAdapter",
        "EmbeddingResult",
        "LLMCapabilities",
        "LLMInvocation",
        "LLMProviderAdapter",
        "LLMRequest",
        "LLMResponse",
        "ProviderCallError",
        "ProviderCapability",
        "ProviderCapabilitySpec",
        "ProviderEndpoint",
        "ProviderFactoryContext",
        "ProviderKey",
        "ProviderManifest",
        "ProviderPlugin",
        "ProviderRequest",
        "ProviderResponse",
        "ProviderStability",
        "RerankInvocation",
        "RerankerProviderAdapter",
        "RerankResult",
        "StructuredOutputMode",
    }
    experimental = {"ImageProviderAdapter", "ImageProviderResult", "ValidatedImageInput"}

    assert stable | experimental <= set(public_api.__all__)


def test_provider_api_snapshot_check_is_clean() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/check_provider_plugin_api.py"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    snapshot = json.loads((ROOT / "docs" / "provider-plugin-api.json").read_text(encoding="utf-8"))
    assert snapshot["entry_point_group"] == "hl_mem.providers"
    assert snapshot["api_version"] == 1
    assert snapshot["experimental"]["capabilities"] == ["image_describer"]
