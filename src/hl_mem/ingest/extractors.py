from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from hl_mem.domain.claims.attributes import (
    infer_canonical_attribute,
    validate_slot_instance,
)


@dataclass(frozen=True)
class ExtractedClaim:
    """提取器输出的单条原子 claim。"""

    predicate: str
    value: str
    confidence: float = 0.9
    volatility: str = "stable"
    subject: str = "用户"
    qualifiers: dict[str, Any] | None = None
    reason: str = ""
    scope: str = "permanent"
    importance: float = 0.5
    canonical_attribute: str = "custom.unknown"
    canonical_slot: str | None = None
    topic_tags: list[str] = field(default_factory=list)


class FakeExtractor:
    """Small deterministic rule extractor used without an LLM or network."""

    patterns = (
        (re.compile(r"现在(?:用|使用)\s*(.+?)(?:[。！!]|$)"), "preference"),
        (re.compile(r"我喜欢\s*(.+?)(?:[。！!]|$)"), "preference"),
        (re.compile(r"记住\s*(.+?)(?:[。！!]|$)"), "explicit_memory"),
        (re.compile(r"使用\s*(.+?)(?:[。！!]|$)"), "uses"),
        (re.compile(r"(.+?现在挂了)(?:[。！!]|$)"), "service_status"),
    )

    def extract(
        self,
        content: str | dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> list[ExtractedClaim]:
        """从内容中确定性提取声明；context 保留用于统一提取器协议。"""
        del context
        content = json.loads(content) if isinstance(content, str) else content
        text = str(content.get("text", ""))
        results: list[ExtractedClaim] = []
        for pattern, predicate in self.patterns:
            if match := pattern.search(text):
                value = match.group(1).strip()
                canonical_attribute = infer_canonical_attribute(predicate, "用户", value)
                qualifiers = {"state_change": True}
                if predicate == "service_status":
                    qualifiers["service"] = value.removesuffix("现在挂了").strip() or "unknown"
                elif not text.startswith("现在"):
                    qualifiers = {}
                results.append(
                    ExtractedClaim(
                        predicate=predicate,
                        value=value,
                        volatility="ephemeral" if predicate == "service_status" else "stable",
                        qualifiers=qualifiers,
                        scope="temporal" if predicate == "service_status" else "permanent",
                        canonical_attribute=canonical_attribute,
                        canonical_slot=validate_slot_instance(canonical_attribute, qualifiers),
                    )
                )
                break
        return results
