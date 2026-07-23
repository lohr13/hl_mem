"""实体标识的确定性归一化。"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any


DEFAULT_ENTITY_ALIASES: dict[str, str] = {
    "hlmem": "hl_mem",
    "hl_mem": "hl_mem",
    "hermes-agent": "Hermes",
    "hermes 插件": "Hermes",
    "hermes memory": "Hermes",
    "codex cli": "Codex",
    "llmextractor": "llm_extractor",
    "watchdog": "hlmem-watchdog",
    "cleanup_data.py": "scripts/cleanup_data.py",
}


def _normalize_text(value: Any, *, casefold: bool) -> str:
    normalized = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value)).strip())
    return normalized.casefold() if casefold else normalized


@lru_cache(maxsize=None)
def _load_aliases(path_value: str | None) -> dict[str, str]:
    if path_value is None:
        raw_aliases: Any = DEFAULT_ENTITY_ALIASES
    else:
        path = Path(path_value)
        try:
            with path.open("r", encoding="utf-8") as handle:
                raw_aliases = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"failed to load entity aliases from {path}: {error}") from error
    if not isinstance(raw_aliases, dict):
        raise ValueError("entity aliases must be a JSON object")

    aliases: dict[str, str] = {}
    for alias, canonical in raw_aliases.items():
        if not isinstance(alias, str) or not isinstance(canonical, str):
            raise ValueError("entity alias keys and values must be strings")
        normalized_alias = _normalize_text(alias, casefold=True)
        normalized_canonical = _normalize_text(canonical, casefold=False)
        if not normalized_alias or not normalized_canonical:
            raise ValueError("entity alias keys and values must not be empty")
        aliases[normalized_alias] = normalized_canonical
    for canonical in tuple(aliases.values()):
        aliases.setdefault(_normalize_text(canonical, casefold=True), canonical)
    return aliases


def normalize_entity_id(subject: str | None) -> str:
    """归一化实体标识，并应用环境变量指定的精确 alias 表。"""
    if subject is None:
        return "unknown"
    normalized = _normalize_text(subject, casefold=True)
    if not normalized:
        return "unknown"
    aliases = _load_aliases(os.getenv("HL_MEM_ENTITY_ALIASES_PATH"))
    return aliases.get(normalized, normalized)
