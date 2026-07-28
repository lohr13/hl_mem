"""按向量相似度清理 active 重复 Claim，并在执行前创建 SQLite 备份。"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from hl_mem.core.vector import cosine_similarity

DEFAULT_DB_PATH = Path(os.getenv("HL_MEM_DB_PATH", "var/hl_mem.db"))


def _value_text(value_json: str | None) -> str:
    """从 value_json 提取可比较文本。"""
    try:
        value = json.loads(value_json or "null")
    except json.JSONDecodeError:
        value = value_json or ""
    if isinstance(value, dict):
        return str(value.get("text") or value)
    return str(value or "")


def _normalize(text: str) -> str:
    """规范化空白、标点、大小写与路径斜杠。"""
    normalized = text.casefold().replace("\\", "/")
    return re.sub(r"[\W_]+", "", normalized, flags=re.UNICODE)


def _is_duplicate(
    left: dict[str, Any], right: dict[str, Any], similarity: float
) -> bool:
    """按规范规定的优先级判断 duplicate/refinement。"""
    left_value = _normalize(_value_text(left["value_json"]))
    right_value = _normalize(_value_text(right["value_json"]))
    if left_value == right_value:
        return True
    if (
        left_value
        and right_value
        and (left_value in right_value or right_value in left_value)
    ):
        return True
    same_subject = left["subject_entity_id"] == right["subject_entity_id"]
    return similarity >= (0.95 if same_subject else 0.97)


def _duplicate_groups(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """按最长 keeper 贪心分组，且要求每个 loser 与 keeper 直接满足规则。"""
    groups: list[list[dict[str, Any]]] = []
    ordered = sorted(
        rows, key=lambda row: (-len(_value_text(row["value_json"])), str(row["id"]))
    )
    for candidate in ordered:
        for group in groups:
            keeper = group[0]
            if candidate["namespace_key"] != keeper["namespace_key"]:
                continue
            same_subject = candidate["subject_entity_id"] == keeper["subject_entity_id"]
            threshold = 0.90 if same_subject else 0.94
            similarity = cosine_similarity(
                candidate["embedding_dense"], keeper["embedding_dense"]
            )
            if similarity >= threshold and _is_duplicate(candidate, keeper, similarity):
                group.append(candidate)
                break
        else:
            groups.append([candidate])
    return [group for group in groups if len(group) > 1]


def _backup_database(db_path: Path, backup_path: Path) -> None:
    """通过 SQLite backup API 创建一致性备份。"""
    if backup_path.exists():
        raise FileExistsError(f"backup already exists: {backup_path}")
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    with (
        closing(sqlite3.connect(db_path)) as source,
        closing(sqlite3.connect(backup_path)) as target,
    ):
        source.backup(target)


def cleanup_duplicates(
    db_path: Path, backup_path: Path, *, dry_run: bool
) -> dict[str, int]:
    """扫描并折叠重复 Claim；dry-run 仅报告，不写库或备份。"""
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    rows = [
        dict(row)
        for row in connection.execute(
            "SELECT id,namespace_key,subject_entity_id,value_json,embedding_dense FROM claims "
            "WHERE status=? AND embedding_dense IS NOT NULL",
            ("active",),
        ).fetchall()
    ]
    groups = _duplicate_groups(rows)
    superseded = sum(len(group) - 1 for group in groups)
    for group in groups:
        keeper = group[0]
        losers = [row for row in group if row["id"] != keeper["id"]]
        print(f"keeper={keeper['id']} value={_value_text(keeper['value_json'])!r}")
        for loser in losers:
            print(
                f"  supersede={loser['id']} value={_value_text(loser['value_json'])!r}"
            )

    if dry_run:
        connection.close()
        print(f"[DRY] groups={len(groups)} superseded={superseded}")
        return {"groups": len(groups), "superseded": superseded}

    if not groups:
        connection.close()
        print("[EXEC] groups=0 superseded=0")
        return {"groups": 0, "superseded": 0}
    connection.close()
    _backup_database(db_path, backup_path)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        for group in groups:
            current_rows = {
                row["id"]: dict(row)
                for row in connection.execute(
                    "SELECT id,namespace_key,subject_entity_id,value_json,embedding_dense,status "
                    f"FROM claims WHERE id IN ({','.join('?' for _ in group)})",
                    tuple(row["id"] for row in group),
                ).fetchall()
            }
            for snapshot in group:
                current = current_rows.get(snapshot["id"])
                comparable = (
                    "namespace_key",
                    "subject_entity_id",
                    "value_json",
                    "embedding_dense",
                )
                if (
                    current is None
                    or current["status"] != "active"
                    or any(current[field] != snapshot[field] for field in comparable)
                ):
                    raise sqlite3.IntegrityError(
                        f"claim changed during cleanup: {snapshot['id']}"
                    )
            keeper = group[0]
            for loser in group:
                if loser["id"] == keeper["id"]:
                    continue
                cursor = connection.execute(
                    "UPDATE claims SET status=?,supersedes_id=?,superseded_by_id=? "
                    "WHERE id=? AND status=?",
                    ("superseded", keeper["id"], keeper["id"], loser["id"], "active"),
                )
                if not cursor.rowcount:
                    raise sqlite3.IntegrityError(
                        f"claim changed during cleanup: {loser['id']}"
                    )
                connection.execute(
                    "INSERT OR IGNORE INTO evidence_links("
                    "id,derived_type,derived_id,evidence_type,evidence_id,relation,weight"
                    ") SELECT lower(hex(randomblob(16))),derived_type,?,evidence_type,evidence_id,relation,weight "
                    "FROM evidence_links WHERE derived_type=? AND derived_id=?",
                    (keeper["id"], "claim", loser["id"]),
                )
                connection.execute(
                    "DELETE FROM evidence_links WHERE derived_type=? AND derived_id=?",
                    ("claim", loser["id"]),
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    print(f"[EXEC] groups={len(groups)} superseded={superseded} backup={backup_path}")
    return {"groups": len(groups), "superseded": superseded}


def main() -> None:
    """解析 CLI 参数并执行重复清理。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="仅打印候选，不修改数据库"
    )
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--backup-path", type=Path)
    args = parser.parse_args()
    backup_path = args.backup_path or args.db_path.with_name(
        f"{args.db_path.name}.bak_cleanup_v3"
    )
    cleanup_duplicates(args.db_path, backup_path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
