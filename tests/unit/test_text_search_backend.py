"""文本检索后端协议与配置测试。"""

from __future__ import annotations

from typing import Any

from hl_mem.protocols import TextSearchBackend
from hl_mem.settings import Settings


class StubTextSearchBackend:
    """用于验证结构化协议边界的文本检索后端。"""

    def search(
        self,
        query: str,
        limit: int,
        reference_time: str,
        intent: Any,
        known_as_of: str | None,
        namespace: str,
    ) -> list[dict]:
        """返回包含检索参数的确定性结果。"""
        return [
            {
                "query": query,
                "limit": limit,
                "reference_time": reference_time,
                "intent": intent,
                "known_as_of": known_as_of,
                "namespace": namespace,
            }
        ]


def _search(backend: TextSearchBackend) -> list[dict]:
    """通过协议类型调用文本检索后端。"""
    return backend.search("中文查询", 5, "2026-07-24T00:00:00+00:00", None, None, "default")


def test_text_search_backend_accepts_structural_implementation() -> None:
    """协议边界允许无需继承的鸭子类型实现。"""
    assert _search(StubTextSearchBackend())[0]["query"] == "中文查询"
