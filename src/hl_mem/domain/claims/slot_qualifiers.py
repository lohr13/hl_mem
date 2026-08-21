"""Slot qualifier 类型边界。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SlotQualifierPolicy:
    """分别声明 slot 准入要求与状态坐标维度。"""

    required: tuple[str, ...] = ()
    coordinate: tuple[str, ...] = ()
