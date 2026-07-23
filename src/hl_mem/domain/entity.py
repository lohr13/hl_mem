"""实体标识的确定性归一化。"""

from __future__ import annotations

import json
import os
import re
import unicodedata
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

_active_aliases: dict[str, str] | None = None


def _normalize_text(value: Any, *, casefold: bool) -> str:
    normalized = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value)).strip())
    return normalized.casefold() if casefold else normalized


def _normalize_aliases(raw_aliases: Any) -> dict[str, str]:
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


def _normalize_default_aliases() -> dict[str, str]:
    """从内置别名构建规范化映射。"""
    return _normalize_aliases(DEFAULT_ENTITY_ALIASES)


def _load_aliases(path_value: str | Path) -> dict[str, str]:
    """从指定 JSON 文件加载并规范化实体别名。"""
    path = Path(path_value)
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw_aliases = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"failed to load entity aliases from {path}: {error}") from error
    return _normalize_aliases(raw_aliases)


def load_entity_aliases(path: str | Path | None = None) -> dict[str, str]:
    """供基础设施层调用：从路径加载实体别名映射。"""
    configured_path = path if path is not None else os.getenv("HL_MEM_ENTITY_ALIASES_PATH")
    if configured_path:
        return _load_aliases(configured_path)
    return _normalize_default_aliases()


def set_active_aliases(aliases: dict[str, str]) -> None:
    """供启动时注入进程级实体别名映射。"""
    global _active_aliases
    _active_aliases = aliases


def normalize_entity_id(subject: str | None, aliases: dict[str, str] | None = None) -> str:
    """归一化实体标识，并应用显式或进程级别名映射。"""
    if subject is None:
        return "unknown"
    normalized = _normalize_text(subject, casefold=True)
    if not normalized:
        return "unknown"
    alias_map = aliases or _active_aliases or _normalize_default_aliases()
    return alias_map.get(normalized, normalized)
