from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from hl_mem.domain.claims.attributes import infer_canonical_attribute


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


class FakeExtractor:
    """Small deterministic rule extractor used without an LLM or network."""

    patterns = (
        (re.compile(r"现在(?:用|使用)\s*(.+?)(?:[。！!]|$)"), "preference"),
        (re.compile(r"我喜欢\s*(.+?)(?:[。！!]|$)"), "preference"),
        (re.compile(r"记住\s*(.+?)(?:[。！!]|$)"), "explicit_memory"),
        (re.compile(r"使用\s*(.+?)(?:[。！!]|$)"), "uses"),
        (re.compile(r"(.+?现在挂了)(?:[。！!]|$)"), "service_status"),
    )

    def extract(self, content_json: str | dict[str, Any]) -> list[ExtractedClaim]:
        content = json.loads(content_json) if isinstance(content_json, str) else content_json
        text = str(content.get("text", ""))
        results: list[ExtractedClaim] = []
        for pattern, predicate in self.patterns:
            if match := pattern.search(text):
                value = match.group(1).strip()
                results.append(
                    ExtractedClaim(
                        predicate=predicate,
                        value=value,
                        volatility="ephemeral" if predicate == "service_status" else "stable",
                        qualifiers={"state_change": True} if text.startswith("现在") else {},
                        scope="temporal" if predicate == "service_status" else "permanent",
                        canonical_attribute=infer_canonical_attribute(predicate, "用户", value),
                    )
                )
                break
        return results
