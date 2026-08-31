#!/usr/bin/env python
"""Generate or verify the versioned Provider Plugin API snapshot."""

from __future__ import annotations

import argparse
import dataclasses
import inspect
import json
import sys
from enum import Enum
from pathlib import Path
from typing import Any, get_type_hints

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
SNAPSHOT = ROOT / "docs" / "provider-plugin-api.json"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

import hl_mem.plugins as api  # noqa: E402

_STABLE_DATACLASSES = (
    "ProviderKey",
    "ProviderEndpoint",
    "ProviderRequest",
    "ProviderResponse",
    "ProviderFactoryContext",
    "ProviderCapabilitySpec",
    "ProviderManifest",
    "ProviderPlugin",
    "LLMCapabilities",
    "LLMRequest",
    "LLMResponse",
    "LLMInvocation",
    "EmbeddingInvocation",
    "EmbeddingResult",
    "RerankInvocation",
    "RerankResult",
)
_EXPERIMENTAL_DATACLASSES = ("ValidatedImageInput", "ImageProviderResult")
_STABLE_PROTOCOLS = (
    ("LLMProviderAdapter", ("build_request", "parse_response", "is_structured_mode_unsupported")),
    ("EmbeddingProviderAdapter", ("build_request", "parse_response")),
    ("RerankerProviderAdapter", ("build_request", "parse_response")),
)
_EXPERIMENTAL_PROTOCOLS = (("ImageProviderAdapter", ("build_request", "parse_response")),)


def _type_name(annotation: Any) -> str:
    if isinstance(annotation, str):
        return annotation
    if annotation is type(None):
        return "None"
    if isinstance(annotation, type):
        return f"{annotation.__module__}.{annotation.__qualname__}"
    return str(annotation).replace("typing.", "").replace("collections.abc.", "")


def _default(field: dataclasses.Field[Any]) -> object:
    if field.default is not dataclasses.MISSING:
        value = field.default
        if isinstance(value, Enum):
            return value.value
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return repr(value)
    if field.default_factory is not dataclasses.MISSING:
        return "<factory>"
    return "<required>"


def _dataclass_contract(name: str) -> list[dict[str, object]]:
    contract = getattr(api, name)
    return [
        {"name": field.name, "type": _type_name(field.type), "default": _default(field)}
        for field in dataclasses.fields(contract)
    ]


def _method_contract(protocol_name: str, method_names: tuple[str, ...]) -> dict[str, object]:
    protocol = getattr(api, protocol_name)
    methods: dict[str, object] = {}
    for method_name in method_names:
        method = getattr(protocol, method_name)
        signature = inspect.signature(method)
        hints = get_type_hints(method)
        methods[method_name] = {
            "parameters": [
                {
                    "name": parameter.name,
                    "kind": parameter.kind.name,
                    "type": _type_name(hints.get(parameter.name, parameter.annotation)),
                    "default": (
                        "<required>" if parameter.default is inspect.Parameter.empty else repr(parameter.default)
                    ),
                }
                for parameter in signature.parameters.values()
            ],
            "return": _type_name(hints.get("return", signature.return_annotation)),
        }
    return methods


def build_snapshot() -> dict[str, object]:
    return {
        "api_version": api.PROVIDER_API_VERSION,
        "entry_point_group": api.PROVIDER_ENTRY_POINT_GROUP,
        "public_exports": sorted(api.__all__),
        "stable": {
            "capabilities": ["llm", "embedding", "reranker"],
            "stability": "stable",
            "dataclasses": {name: _dataclass_contract(name) for name in _STABLE_DATACLASSES},
            "protocols": {name: _method_contract(name, methods) for name, methods in _STABLE_PROTOCOLS},
        },
        "experimental": {
            "capabilities": ["image_describer"],
            "stability": "experimental",
            "dataclasses": {name: _dataclass_contract(name) for name in _EXPERIMENTAL_DATACLASSES},
            "protocols": {name: _method_contract(name, methods) for name, methods in _EXPERIMENTAL_PROTOCOLS},
        },
        "enums": {
            "ProviderCapability": [item.value for item in api.ProviderCapability],
            "ProviderStability": [item.value for item in api.ProviderStability],
            "StructuredOutputMode": [item.value for item in api.StructuredOutputMode],
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    rendered = json.dumps(build_snapshot(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.write:
        SNAPSHOT.write_text(rendered, encoding="utf-8")
        print(f"wrote {SNAPSHOT.relative_to(ROOT)}")
        return 0
    if not SNAPSHOT.is_file():
        print(f"Provider Plugin API snapshot is missing: {SNAPSHOT.relative_to(ROOT)}")
        return 1
    if SNAPSHOT.read_text(encoding="utf-8") != rendered:
        print("Provider Plugin API snapshot is stale; review the contract and run with --write")
        return 1
    print(f"Provider Plugin API snapshot matches version {api.PROVIDER_API_VERSION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
