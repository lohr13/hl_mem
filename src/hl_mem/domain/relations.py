"""记忆关系领域模型与轻量持久化操作。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class RelationType(StrEnum):
    """支持的记忆关系类型。"""

    SUMMARIZES = "summarizes"
    SUPPORTS = "supports"
    FOLLOWS = "follows"
    ABOUT = "about"
    CONTRADICTS = "contradicts"


def add_relation(
    connection: sqlite3.Connection,
    from_id: str,
    to_id: str,
    relation: RelationType | str,
    confidence: float = 1.0,
) -> str:
    """创建一条 claim 关系并返回其标识。"""
    relation_type = RelationType(relation)
    relation_id = uuid.uuid4().hex
    connection.execute(
        "INSERT INTO memory_relations "
        "(id,from_id,to_id,relation,confidence,evidence_json,created_at) VALUES (?,?,?,?,?,?,?)",
        (
            relation_id,
            from_id,
            to_id,
            relation_type.value,
            min(1.0, max(0.0, float(confidence))),
            json.dumps([], ensure_ascii=False),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    connection.commit()
    return relation_id


def get_relations(
    connection: sqlite3.Connection,
    claim_id: str,
    direction: str = "both",
) -> list[dict[str, Any]]:
    """按出向、入向或双向查询指定 claim 的关系。"""
    if direction not in {"from", "to", "both"}:
        raise ValueError("direction must be 'from', 'to', or 'both'")
    clauses: list[str] = []
    parameters: list[str] = []
    if direction in {"from", "both"}:
        clauses.append("from_id=?")
        parameters.append(claim_id)
    if direction in {"to", "both"}:
        clauses.append("to_id=?")
        parameters.append(claim_id)
    rows = connection.execute(
        "SELECT id,from_id,to_id,relation,confidence,evidence_json,created_at "
        f"FROM memory_relations WHERE {' OR '.join(clauses)} ORDER BY created_at,id",
        parameters,
    ).fetchall()
    return [dict(row) for row in rows]
