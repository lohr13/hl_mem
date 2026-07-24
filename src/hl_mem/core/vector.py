"""纯向量数学函数，不依赖业务包。"""

from __future__ import annotations

import math

from hl_mem.ingest.embedder import unpack_vector


def cosine_similarity(query_blob: bytes, target_blob: bytes) -> float:
    """计算两个 float32 向量 BLOB 的余弦相似度。"""
    query = unpack_vector(query_blob)
    target = unpack_vector(target_blob)
    if len(query) != len(target):
        raise ValueError("embedding dimensions differ")
    denominator = math.sqrt(sum(value * value for value in query) * sum(value * value for value in target))
    return sum(left * right for left, right in zip(query, target)) / denominator if denominator else 0.0
