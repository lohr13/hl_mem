from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExtractedClaim:
    predicate: str
    value: str
    confidence: float = 0.9
    volatility: str = "stable"
    subject: str = "用户"
    qualifiers: dict[str, Any] | None = None
    reason: str = ""


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
                results.append(
                    ExtractedClaim(
                        predicate=predicate,
                        value=match.group(1).strip(),
                        volatility="ephemeral" if predicate == "service_status" else "stable",
                        qualifiers={"state_change": True} if text.startswith("现在") else {},
                    )
                )
                break
        return results


class FakeEmbedder:
    """Return a deterministic pseudo-random vector for repeatable tests."""

    def __init__(self, dimension: int = 16) -> None:
        self.dimension = dimension

    def embed(self, text: str) -> list[float]:
        generator = random.Random(text)
        return [generator.uniform(-1.0, 1.0) for _ in range(self.dimension)]
