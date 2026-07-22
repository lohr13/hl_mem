"""纯向量数学函数。不依赖任何业务包。"""

from __future__ import annotations

import math
import struct


def encode_vector(vec: list[float]) -> bytes:
    """将 float 列表序列化为小端 float32 字节串。"""
    return struct.pack(f"<{len(vec)}f", *vec)


def decode_vector(blob: bytes) -> list[float]:
    """将小端 float32 字节串反序列化为 float 列表。"""
    if len(blob) % 4:
        raise ValueError("vector BLOB length must be divisible by four")
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def cosine_similarity(query_blob: bytes, target_blob: bytes) -> float:
    """计算两个序列化 float32 向量的余弦相似度。"""
    query = decode_vector(query_blob)
    target = decode_vector(target_blob)
    if len(query) != len(target):
        raise ValueError("embedding dimensions differ")
    denominator = math.sqrt(sum(value * value for value in query) * sum(value * value for value in target))
    return sum(left * right for left, right in zip(query, target)) / denominator if denominator else 0.0
