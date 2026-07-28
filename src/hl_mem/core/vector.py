"""纯向量数学函数，不依赖业务包。"""

from __future__ import annotations

import math
import struct
from collections.abc import Sequence
from typing import Iterable

import numpy as np
import numpy.typing as npt


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
    denominator = math.sqrt(sum(value * value for value in query) * sum(value * value for value in target))
    return sum(left * right for left, right in zip(query, target)) / denominator if denominator else 0.0


def batch_cosine_similarity(query_blob: bytes, target_blobs: Sequence[bytes], batch_size: int = 512) -> list[float]:
    """分批计算一个查询向量与多个 float32 BLOB 的精确余弦相似度。"""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if len(query_blob) % 4:
        raise ValueError("embedding BLOB length must be divisible by four")

    query = np.frombuffer(query_blob, dtype="<f4")
    dimension = query.size
    query_norm = np.linalg.norm(query)
    normalized_query: npt.NDArray[np.float32]
    if query_norm == 0.0:
        normalized_query = np.zeros(dimension, dtype=np.float32)
    else:
        normalized_query = query / query_norm

    scores: list[float] = []
    for start in range(0, len(target_blobs), batch_size):
        blobs = target_blobs[start : start + batch_size]
        matrix = np.empty((len(blobs), dimension), dtype=np.float32)
        for row_index, blob in enumerate(blobs):
            if len(blob) % 4:
                raise ValueError("embedding BLOB length must be divisible by four")
            target = np.frombuffer(blob, dtype="<f4")
            if target.size != dimension:
                raise ValueError("embedding dimensions differ")
            matrix[row_index] = target

        norms = np.linalg.norm(matrix, axis=1)
        batch_scores = np.zeros(len(blobs), dtype=np.float32)
        np.divide(matrix @ normalized_query, norms, out=batch_scores, where=norms != 0.0)
        scores.extend(float(score) for score in batch_scores)
    return scores


def normalized_vector(blob: bytes) -> tuple[float, ...]:
    """解码并归一化一个 float32 向量 BLOB。"""
    vector = unpack_vector(blob)
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return tuple(0.0 for _ in vector)
    return tuple(value / norm for value in vector)


def normalized_cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """计算两个已归一化向量的点积相似度。"""
    if len(left) != len(right):
        raise ValueError("embedding dimensions differ")
    return sum(left_value * right_value for left_value, right_value in zip(left, right))
