"""Source-bounded coordinates for model-choice Claims."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_TASK_ALIASES: dict[str, tuple[str, ...]] = {
    "reader": ("reader", "阅读"),
    "answering": ("question answering", "answering", "qa", "问答", "回答"),
    "judge": ("judge", "评审", "裁判"),
    "extraction": ("memory_extraction", "memory extraction", "extractor", "extraction", "提取", "抽取"),
    "embedding": ("embedding", "嵌入"),
    "reranker": ("reranker", "reranking", "重排"),
    "summarization": ("summarization", "摘要"),
    "compression": ("compression", "压缩"),
    "translation": ("translation", "翻译"),
    "code_generation": ("code generation", "代码生成"),
    "image_generation": ("image generation", "图像生成"),
    "vision": ("vision", "视觉"),
    "verification": ("verification", "验证"),
    "testing": ("testing", "测试"),
}

MODEL_TASK_SOURCE_MARKERS = tuple(dict.fromkeys(alias for aliases in _TASK_ALIASES.values() for alias in aliases))

_CURRENTNESS_MARKERS = (
    "当前实际",
    "当前",
    "现在",
    "目前",
    "实际使用",
    "已切换",
    "切换为",
    "currently",
    "now uses",
    "now use",
    "switched to",
)

_HL_MEM_SUBJECT_PREFIX = re.compile(r"^hl(?:[_\-\s]?mem)(?:['’]s|的)?", re.IGNORECASE)
_SUBJECT_DECORATORS = (
    "configuration",
    "pipeline",
    "runtime",
    "service",
    "config",
    "memory",
    "model",
    "local",
    "task",
    "本地",
    "记忆",
    "任务",
    "模型",
    "配置",
    "服务",
)


@dataclass(frozen=True)
class ModelCoordinateProjection:
    subject: str
    task: str | None
    state_change: bool


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).strip().casefold())


def _contains_alias(text: str, alias: str) -> bool:
    if alias.isascii() and all(character.isalnum() or character in {"_", " "} for character in alias):
        return re.search(rf"(?<![a-z0-9_]){re.escape(alias)}(?![a-z0-9_])", text) is not None
    return alias in text


def _matched_tasks(subject: str, evidence_quote: str) -> tuple[str, ...]:
    evidence = _normalize(evidence_quote)
    normalized_subject = _normalize(subject)
    return tuple(
        task
        for task, aliases in _TASK_ALIASES.items()
        if any(_contains_alias(evidence, alias) for alias in aliases)
        and any(_contains_alias(normalized_subject, alias) for alias in aliases)
    )


def _is_current(value: str, evidence_quote: str) -> bool:
    normalized_value = _normalize(value)
    normalized_evidence = _normalize(evidence_quote)
    return any(marker in normalized_value and marker in normalized_evidence for marker in _CURRENTNESS_MARKERS)


def _canonical_hl_mem_subject(subject: str, task: str | None) -> str:
    if task is None:
        return subject
    normalized = _normalize(subject)
    match = _HL_MEM_SUBJECT_PREFIX.match(normalized)
    if match is None:
        return subject
    remainder = normalized[match.end() :]
    removable = sorted((*_TASK_ALIASES[task], *_SUBJECT_DECORATORS), key=len, reverse=True)
    for marker in removable:
        remainder = remainder.replace(marker, "")
    remainder = re.sub(r"[\s._/:'’\-]+", "", remainder)
    return "hl_mem" if not remainder else subject


def project_model_coordinates(
    attribute: str,
    subject: str,
    value: str,
    evidence_quote: str,
) -> ModelCoordinateProjection:
    """Project source-proven model-task coordinates; ambiguity stays uncoordinated."""
    if attribute != "choice.model":
        return ModelCoordinateProjection(subject, None, False)
    tasks = _matched_tasks(subject, evidence_quote)
    task = tasks[0] if len(tasks) == 1 else None
    return ModelCoordinateProjection(
        _canonical_hl_mem_subject(subject, task),
        task,
        bool(task and _is_current(value, evidence_quote)),
    )
