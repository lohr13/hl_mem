"""实体标识的确定性归一化。"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

PERSONA_ENTITY_ALIASES: dict[str, str] = {
    "我": "user",
    "本人": "user",
    "i": "user",
    "me": "user",
    "myself": "user",
    "user": "user",
    "the user": "user",
    "current user": "user",
    "the current user": "user",
    "用户": "user",
    "当前用户": "user",
}

DEFAULT_ENTITY_ALIASES: dict[str, str] = {
    **PERSONA_ENTITY_ALIASES,
    "hlmem": "hl_mem",
    "hl_mem": "hl_mem",
    "hl_mem 项目": "hl_mem",
    "hl_mem项目": "hl_mem",
    "hl_mem 服务": "hl_mem",
    "hl_mem_plugin": "hl_mem",
    "hl-mem": "hl_mem",
    "hermes-agent": "Hermes",
    "hermes 插件": "Hermes",
    "hermes memory": "Hermes",
    "codex cli": "Codex",
    "llmextractor": "llm_extractor",
    "watchdog": "hlmem-watchdog",
}

_active_aliases: dict[str, str] | None = None
_FILE_SUBJECT_PATTERN = re.compile(r"(?i)^.+\.py$")
_PASCAL_CASE_SUBJECT_PATTERN = re.compile(r"^(?:[A-Z][a-z0-9]+){2,}$")
_ENVIRONMENT_VARIABLE_PATTERN = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")


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
    aliases = _normalize_default_aliases()
    if path is not None:
        aliases.update(_load_aliases(path))
    return aliases


def set_active_aliases(aliases: dict[str, str]) -> None:
    """供启动时注入进程级实体别名映射。"""
    global _active_aliases
    _active_aliases = _normalize_aliases(aliases)


def _resolved_aliases(aliases: dict[str, str] | None) -> dict[str, str]:
    if aliases is not None:
        return _normalize_aliases(aliases)
    return _active_aliases or _normalize_default_aliases()


def normalize_entity_alias(subject: str | None, aliases: dict[str, str] | None = None) -> str:
    """只应用已知别名，并保留未列入表中的实体显示形式。"""
    if subject is None:
        return "unknown"
    normalized = _normalize_text(subject, casefold=False)
    if not normalized:
        return "unknown"
    return _resolved_aliases(aliases).get(normalized.casefold(), normalized)


def normalize_entity_id(subject: str | None, aliases: dict[str, str] | None = None) -> str:
    """归一化 namespace 内的实体标签，并应用显式或进程级别名映射。"""
    if subject is None:
        return "unknown"
    normalized = _normalize_text(subject, casefold=True)
    if not normalized:
        return "unknown"
    return _resolved_aliases(aliases).get(normalized, normalized)


def invalid_subject_reason(subject: str | None) -> str | None:
    """判断候选是否属于不允许作为顶层 subject 的技术标识。"""
    if subject is None:
        return "empty"
    normalized = _normalize_text(subject, casefold=False)
    if not normalized:
        return "empty"
    if "/" in normalized or "\\" in normalized:
        return "path"
    if _FILE_SUBJECT_PATTERN.fullmatch(normalized):
        return "filename"
    if _ENVIRONMENT_VARIABLE_PATTERN.fullmatch(normalized):
        return "environment_variable"
    if _PASCAL_CASE_SUBJECT_PATTERN.fullmatch(normalized):
        return "class_name"
    return None


def isolated_subject_id(*identity_parts: Any) -> str:
    """为无法归属到合法实体的 claim 生成稳定且互相隔离的主体标识。"""
    payload = json.dumps(identity_parts, ensure_ascii=False, separators=(",", ":"), default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"unknown__{digest}"
