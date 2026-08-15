"""用户显式删除入口，委托统一的物理删除闭包。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hl_mem.application.deletion import DeletionService


class ForgetService:
    """保持既有 forget 适配面，同时下沉业务逻辑到 DeletionService。"""

    def __init__(self, connection: Any, *, ledger_path: str | Path | None = None) -> None:
        self.connection = connection
        self.ledger_path = ledger_path

    def forget(self, memory_id: str) -> dict[str, Any]:
        """写入墓碑后物理删除 claim 及其专属闭包。"""
        result = DeletionService(
            self.connection,
            ledger_path=self.ledger_path,
        ).delete_claim(memory_id)
        return {
            "id": result.claim_id,
            "forgotten": True,
            "already_deleted": result.already_deleted,
            "tombstone_hash": result.identity_hash,
            "deleted_event_ids": list(result.deleted_event_ids),
        }
