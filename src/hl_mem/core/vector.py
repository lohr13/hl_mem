"""纯向量数学函数，不依赖业务包。"""

from __future__ import annotations

import math
import struct
from collections.abc import Sequence
from typing import Iterable


def pack_vector(values: Iterable[float]) -> bytes:
    """将浮点序列编码为小端 float32 BLOB。"""
    materialized = list(values)
    return struct.pack(f"<{len(materialized)}f", *materialized)


def unpack_vector(blob: bytes) -> tuple[float, ...]:
    """将小端 float32 BLOB 解码为浮点元组。"""
    if len(blob) % 4:
        raise ValueError("embedding BLOB length must be divisible by four")
    return struct.unpack(f"<{len(blob) // 4}f", blob)


def cosine_similarity(query_blob: bytes, target_blob: bytes) -> float:
    """计算两个 float32 向量 BLOB 的余弦相似度。"""
    query = unpack_vector(query_blob)
    target = unpack_vector(target_blob)
    if len(query) != len(target):
        raise ValueError("embedding dimensions differ")
    denominator = math.sqrt(
        sum(value * value for value in query) * sum(value * value for value in target)
    )
    return (
        sum(left * right for left, right in zip(query, target)) / denominator
        if denominator
        else 0.0
    )


def normalized_vector(blob: bytes) -> tuple[float, ...]:
    """解码并归一化一个 float32 向量 BLOB。"""
    vector = unpack_vector(blob)
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return tuple(0.0 for _ in vector)
    return tuple(value / norm for value in vector)


def normalized_cosine_similarity(
    left: Sequence[float], right: Sequence[float]
) -> float:
    """计算两个已归一化向量的点积相似度。"""
    if len(left) != len(right):
        raise ValueError("embedding dimensions differ")
    return sum(left_value * right_value for left_value, right_value in zip(left, right))
