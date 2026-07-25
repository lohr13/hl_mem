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


def get_relations_batch(
    connection: sqlite3.Connection,
    claim_ids: list[str],
    *,
    include_memory_relations: bool = False,
    include_reverse_evidence: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """批量获取多个 claim 的 evidence relations，并按 claim 标识分组。"""
    unique_ids = list(dict.fromkeys(claim_ids))
    if not unique_ids:
        return {}
    result: dict[str, list[dict[str, Any]]] = {claim_id: [] for claim_id in unique_ids}
    for start in range(0, len(unique_ids), 500):
        chunk = unique_ids[start : start + 500]
        placeholders = ",".join("?" for _ in chunk)
        extended = include_memory_relations or include_reverse_evidence
        columns = "derived_id,relation,evidence_type,evidence_id,weight" if extended else (
            "derived_id,relation,evidence_type,evidence_id"
        )
        allowed = "'supports','contradicts','follows','about','derived_from','supersedes'" if extended else (
            "'supports','contradicts','follows','about'"
        )
        rows = connection.execute(
            f"SELECT {columns} FROM evidence_links WHERE derived_type='claim' "
            f"AND relation IN ({allowed}) "
            f"AND derived_id IN ({placeholders})",
            chunk,
        ).fetchall()
        for row in rows:
            relation = dict(row)
            if extended:
                relation.update(
                    seed_id=relation["derived_id"],
                    neighbor_id=relation["evidence_id"] if relation["evidence_type"] == "claim" else None,
                    source="evidence_links",
                    confidence=float(relation.get("weight") or 1.0),
                )
            result[relation["derived_id"]].append(relation)
        if include_reverse_evidence:
            reverse_rows = connection.execute(
                "SELECT derived_id,relation,evidence_id,weight "
                "FROM evidence_links WHERE evidence_type='claim' AND derived_type='claim' "
                "AND relation IN ('supports','contradicts','follows','about','derived_from','supersedes') "
                f"AND evidence_id IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in reverse_rows:
                relation = dict(row)
                result[relation["evidence_id"]].append(
                    {
                        **relation,
                        "seed_id": relation["evidence_id"],
                        "neighbor_id": relation["derived_id"],
                        "source": "evidence_links",
                        "confidence": float(relation.get("weight") or 1.0),
                    }
                )
        if include_memory_relations:
            memory_rows = connection.execute(
                "SELECT from_id,to_id,relation,confidence FROM memory_relations "
                f"WHERE from_id IN ({placeholders}) OR to_id IN ({placeholders})",
                (*chunk, *chunk),
            ).fetchall()
            chunk_ids = set(chunk)
            for row in memory_rows:
                relation = dict(row)
                for seed_id, neighbor_id in (
                    (relation["from_id"], relation["to_id"]),
                    (relation["to_id"], relation["from_id"]),
                ):
                    if seed_id in chunk_ids:
                        result[seed_id].append(
                            {
                                **relation,
                                "seed_id": seed_id,
                                "neighbor_id": neighbor_id,
                                "source": "memory_relations",
                            }
                        )
    return result


def walk_relation_graph(
    connection: sqlite3.Connection,
    seed_ids: list[str],
    namespace: str,
    max_depth: int = 1,
    allowed_relations: list[RelationType | str] | None = None,
    min_confidence: float = 0.0,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """使用递归 CTE 在 namespace 内双向遍历关系图。"""
    if not 1 <= max_depth <= 3:
        raise ValueError("max_depth must be between 1 and 3")
    if not 0.0 <= min_confidence <= 1.0:
        raise ValueError("min_confidence must be between 0 and 1")
    if limit < 1:
        raise ValueError("limit must be positive")
    seeds = list(dict.fromkeys(seed_ids))
    if not seeds:
        return []
    relations = [RelationType(value).value for value in (allowed_relations or list(RelationType))]
    if not relations:
        return []
    seed_placeholders = ",".join("?" for _ in seeds)
    relation_placeholders = ",".join("?" for _ in relations)
    rows = connection.execute(
        f"""
        WITH RECURSIVE graph(seed_id,node_id,depth,path,weight) AS (
            SELECT c.id,c.id,0,'|' || c.id || '|',1.0
            FROM claims AS c
            WHERE c.id IN ({seed_placeholders}) AND c.namespace_key=?
            UNION ALL
            SELECT graph.seed_id,
                   CASE WHEN mr.from_id=graph.node_id THEN mr.to_id ELSE mr.from_id END,
                   graph.depth+1,
                   graph.path || CASE WHEN mr.from_id=graph.node_id THEN mr.to_id ELSE mr.from_id END || '|',
                   graph.weight*mr.confidence
            FROM graph
            JOIN memory_relations AS mr ON mr.from_id=graph.node_id OR mr.to_id=graph.node_id
            JOIN claims AS next_claim
              ON next_claim.id=CASE WHEN mr.from_id=graph.node_id THEN mr.to_id ELSE mr.from_id END
            WHERE graph.depth<?
              AND mr.relation IN ({relation_placeholders})
              AND mr.confidence>=?
              AND next_claim.namespace_key=?
              AND instr(graph.path,'|' || next_claim.id || '|')=0
        )
        SELECT seed_id,node_id,depth,path,weight
        FROM graph WHERE depth>0
        ORDER BY weight DESC,depth ASC,node_id ASC,seed_id ASC
        LIMIT ?
        """,
        (*seeds, namespace, max_depth, *relations, min_confidence, namespace, limit),
    ).fetchall()
    return [dict(row) for row in rows]
