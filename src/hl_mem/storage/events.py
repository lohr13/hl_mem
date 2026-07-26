"""事件仓储。"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from hl_mem.protocols import ImageDescription
from hl_mem.storage._shared import (
    decode_json,
    encode_json,
    insert_row,
    is_fts_syntax_error,
    row_to_dict,
    sanitize_fts_query,
)


class EventRepository:
    """提供事件写入、读取和全文检索。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def insert_event(self, event: dict[str, Any], commit: bool = True) -> bool:
        """写入事件，并在需要时提交事务。"""
        stored = dict(event)
        if "content" in stored:
            content_json = encode_json(stored.pop("content"), sort_keys=True)
            stored["content_json"] = content_json
            stored.setdefault("content_hash", hashlib.sha256(content_json.encode()).hexdigest())
        if "metadata" in stored:
            stored["metadata_json"] = encode_json(stored.pop("metadata"), sort_keys=True)
        return insert_row(self.connection, "events", stored, commit)

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        """按标识返回事件。"""
        event = row_to_dict(self.connection.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone())
        if event is not None:
            event["content"] = decode_json(event["content_json"])
            if event.get("metadata_json") is not None:
                event["metadata"] = decode_json(event["metadata_json"])
        return event

    def find_id_by_idempotency_key(self, idempotency_key: str) -> str | None:
        """按幂等键返回已存在的事件标识。"""
        row = self.connection.execute("SELECT id FROM events WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        return str(row["id"]) if row else None

    def insert_image_description_event(
        self,
        source_event: dict[str, Any],
        image_index: int,
        description: ImageDescription,
        *,
        commit: bool = True,
    ) -> dict[str, Any]:
        """幂等写入图片描述派生事件，并返回已存在或新建事件。"""
        image_hash = (
            description.locator.sha256
            or hashlib.sha256((description.locator.uri or f"image-{image_index}").encode()).hexdigest()
        )
        idempotency_key = f"image-describe:{source_event['id']}:{image_hash}:{description.model}"
        existing_id = self.find_id_by_idempotency_key(idempotency_key)
        if existing_id:
            existing = self.get_event(existing_id)
            if existing is None:
                raise RuntimeError(f"idempotent image description event disappeared: {existing_id}")
            return existing
        event_id = uuid.uuid4().hex
        event = {
            "id": event_id,
            "idempotency_key": idempotency_key,
            "tenant_id": source_event.get("tenant_id", "default"),
            "user_id": source_event.get("user_id"),
            "project_id": source_event.get("project_id"),
            "agent_id": source_event.get("agent_id"),
            "session_id": source_event.get("session_id"),
            "event_type": "image_description",
            "actor_type": "system",
            "content": {
                "source_event_id": source_event["id"],
                "image_index": image_index,
                "caption": description.caption,
                "ocr_text": description.ocr_text,
                "model": description.model,
                "confidence": description.confidence,
                "locator": asdict(description.locator),
            },
            "occurred_at": source_event["occurred_at"],
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "source_uri": description.locator.uri,
            "sensitivity": source_event.get("sensitivity", "normal"),
        }
        self.insert_event(event, commit=commit)
        stored = self.get_event(event_id)
        if stored is None:
            raise RuntimeError(f"failed to read inserted image description event: {event_id}")
        return stored

    def find_image_description(self, source_event_id: str, image_index: int, model: str) -> dict[str, Any] | None:
        """按来源、图片序号和模型查找可复用的派生描述事件。"""
        row = self.connection.execute(
            "SELECT id FROM events WHERE event_type='image_description' "
            "AND json_extract(content_json, '$.source_event_id')=? "
            "AND json_extract(content_json, '$.image_index')=? "
            "AND json_extract(content_json, '$.model')=? ORDER BY recorded_at DESC LIMIT 1",
            (source_event_id, image_index, model),
        ).fetchone()
        return self.get_event(str(row["id"])) if row else None

    def get_recent_events(self, session_id: str, before: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        """返回游标之前的最近事件。"""
        rows = self.connection.execute(
            "SELECT * FROM events WHERE session_id=? AND "
            "(occurred_at<? OR (occurred_at=? AND id<?)) "
            "ORDER BY occurred_at DESC,id DESC LIMIT ?",
            (session_id, before["occurred_at"], before["occurred_at"], before["id"], limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def search_events_fts(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """执行安全的事件全文检索。"""
        try:
            rows = self.connection.execute(
                "SELECT e.* FROM events_fts f JOIN events e ON e.rowid=f.rowid "
                "WHERE events_fts MATCH ? ORDER BY bm25(events_fts) LIMIT ?",
                (sanitize_fts_query(query), limit),
            ).fetchall()
        except sqlite3.OperationalError as error:
            if not is_fts_syntax_error(error):
                raise
            return []
        return [dict(row) for row in rows]
