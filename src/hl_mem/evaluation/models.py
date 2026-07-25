"""Benchmark 输入、时间 gold 与适配器协议。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol


@dataclass(frozen=True)
class GoldTemporal:
    """用于 extraction/retrieval 时间正确性的 gold 区间。"""

    evidence_event_id: str
    occurred_start: str | None
    occurred_end: str | None
    valid_from: str | None
    valid_to: str | None


@dataclass(frozen=True)
class LifecycleCheckpoint:
    """某个双时间检查点下的期望可见性和状态。"""

    at: str
    known_as_of: str | None
    expected_visible_event_ids: tuple[str, ...]
    expected_hidden_event_ids: tuple[str, ...]
    expected_status_by_event_id: dict[str, str]
    worker_action: str | None


@dataclass(frozen=True)
class BenchmarkCase:
    """规范化后的长期记忆评测样本。"""

    case_id: str
    events: tuple[dict[str, object], ...]
    query: str
    gold_evidence_event_ids: tuple[str, ...]
    gold_temporal: tuple[GoldTemporal, ...]
    lifecycle_checkpoints: tuple[LifecycleCheckpoint, ...]
    gold_answer: str | None
    as_of: str | None
    known_as_of: str | None
    category: str


class BenchmarkAdapterProtocol(Protocol):
    """把公开数据转换为 hl_mem 事件和 gold 约束。"""

    def load(self, source: Path, subset: str) -> Iterable[BenchmarkCase]:
        """加载并规范化指定 benchmark 子集。"""
        ...
