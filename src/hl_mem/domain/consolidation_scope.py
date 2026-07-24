"""归并任务的显式作用域。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConsolidationScope:
    """限定一次 claim 归并扫描的命名空间、分类范围和候选规模。"""

    namespace: str = "default"
    slot_filter: str | None = None
    tag_filter: list[str] | None = None
    max_pairs: int = 500
    similarity_threshold: float = 0.72
    similarity_ceiling: float = 0.95
