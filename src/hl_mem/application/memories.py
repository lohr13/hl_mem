"""记忆分页查询应用服务。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from hl_mem.storage.claims import ClaimRepository


class MemoryQueryService:
    """通过统一仓储路径查询可公开展示的 Claim 页。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def list_memories(
        self,
        *,
        namespace: str = "default",
        status: str = "active",
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        claims, total = ClaimRepository(self.connection).list_memories(namespace, status, limit, offset)
        return {
            "memories": [self._public_item(claim) for claim in claims],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @staticmethod
    def _public_item(claim: dict[str, Any]) -> dict[str, Any]:
        value = claim.get("value")
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
        return {
            "id": str(claim["id"]),
            "text": text,
            "status": str(claim["status"]),
            "recorded_from": str(claim["recorded_from"]),
            "valid_from": claim.get("valid_from"),
            "canonical_slot": claim.get("canonical_slot"),
            "topic_tags": list(claim.get("topic_tags") or []),
        }
