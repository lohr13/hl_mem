#!/usr/bin/env python
"""Generate or verify the stable Core 1.0 configuration contract snapshot."""

from __future__ import annotations

import argparse
import json
import types
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from hl_mem.config.loader import PLUGIN_ID_PATTERN, REQUIRED_RUNTIME_PATHS, RETIRED_TOML_PATHS
from hl_mem.config.models import CONFIG_SCHEMA_VERSION, Settings, iter_config_fields

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "docs" / "config-schema.json"
_PRODUCTION_CHOICE_EXCLUSIONS = {
    "embedding.mode": {"fake"},
    "extraction.mode": {"fake"},
    "reranker.mode": {"fake"},
}


def _type_name(annotation: Any) -> str:
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is Literal:
        return _type_name(type(arguments[0]))
    if origin is tuple:
        item_type = _type_name(arguments[0]) if arguments else "any"
        return f"array[{item_type}]"
    if origin in {types.UnionType, Union}:
        non_none = [item for item in arguments if item is not type(None)]
        if len(non_none) == 1:
            return _type_name(non_none[0])
        return " | ".join(_type_name(item) for item in non_none)
    if isinstance(annotation, type) and issubclass(annotation, StrEnum):
        return "string"
    return {str: "string", int: "integer", float: "number", bool: "boolean"}.get(
        annotation,
        str(annotation),
    )


def _choices(annotation: Any) -> list[object] | None:
    origin = get_origin(annotation)
    if origin is Literal:
        return list(get_args(annotation))
    if isinstance(annotation, type) and issubclass(annotation, StrEnum):
        return [item.value for item in annotation]
    return None


def _json_default(value: Any) -> object:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, tuple):
        return [_json_default(item) for item in value]
    return value


def build_config_schema() -> dict[str, object]:
    """Build the stable, secret-free public configuration description."""
    annotations = get_type_hints(Settings)
    fields: list[dict[str, object]] = []
    secrets: list[dict[str, str]] = []
    for item in iter_config_fields():
        toml_path = item.metadata.get("toml")
        secret_name = item.metadata.get("secret_env")
        if toml_path is not None:
            path = str(toml_path)
            annotation = annotations[item.name]
            choices = _choices(annotation)
            if choices is not None:
                choices = [value for value in choices if value not in _PRODUCTION_CHOICE_EXCLUSIONS.get(path, set())]
            entry: dict[str, object] = {
                "default": _json_default(item.default),
                "path": path,
                "required_in_production": path in REQUIRED_RUNTIME_PATHS,
                "settings_field": item.name,
                "type": _type_name(annotation),
            }
            if choices is not None:
                entry["production_choices"] = choices
            fields.append(entry)
        elif secret_name is not None:
            secrets.append({"environment": str(secret_name), "settings_field": item.name})

    return {
        "fields": sorted(fields, key=lambda item: str(item["path"])),
        "open_namespaces": {
            "plugins.<id>": {
                "id_pattern": PLUGIN_ID_PATTERN.pattern,
                "value": "plugin-defined non-secret TOML table",
            }
        },
        "required_production_paths": list(REQUIRED_RUNTIME_PATHS),
        "retired_paths": sorted(RETIRED_TOML_PATHS),
        "schema_version": CONFIG_SCHEMA_VERSION,
        "secrets": sorted(secrets, key=lambda item: item["environment"]),
    }


def rendered_schema() -> str:
    return json.dumps(build_config_schema(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    rendered = rendered_schema()
    if args.write:
        SNAPSHOT.write_text(rendered, encoding="utf-8")
        print(f"Config schema snapshot updated: {SNAPSHOT.relative_to(ROOT)}")
        return 0
    if not SNAPSHOT.is_file() or SNAPSHOT.read_text(encoding="utf-8") != rendered:
        print("Config schema snapshot mismatch; run scripts/check_config_schema_snapshot.py --write")
        return 1
    print("Config schema snapshot check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
