"""为 Phase 17 slot、tags 和新冲突键生成 dry-run 统计，并可显式应用回填。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hl_mem.domain.claims.attributes import ALLOWED_TOPIC_TAGS, validate_canonical_slot
from hl_mem.domain.claims.conflicts import compute_conflict_key
from hl_mem.storage.database import default_database_path


@dataclass(frozen=True)
class ClaimSlotBackfillStats:
    """记录 claim slot 回填的确定性统计。"""

    total: int
    operational: int
    null_slot: int
    tag_counts: dict[str, int]
    applied: bool


def _tags_for_attribute(attribute: str) -> list[str]:
    """把旧 attribute 尽可能无损地映射到受控 topic tags。"""
    domain, _, suffix = attribute.partition(".")
    candidates = [domain, suffix]
    if suffix == "project_membership":
        candidates.append("membership")
    return list(dict.fromkeys(tag for tag in candidates if tag in ALLOWED_TOPIC_TAGS))


def _decode_qualifiers(claim_id: str, raw: Any) -> dict[str, Any]:
    """解码回填所需 qualifier，并拒绝损坏的历史数据。"""
    try:
        qualifiers = json.loads(raw or "{}") if isinstance(raw, str) else (raw or {})
    except json.JSONDecodeError as error:
        raise ValueError(f"claim {claim_id} has invalid qualifiers_json") from error
    if not isinstance(qualifiers, dict):
        raise ValueError(f"claim {claim_id} qualifiers_json must be an object")
    return qualifiers


def backfill_claim_slots_v1(
    connection: sqlite3.Connection,
    *,
    apply: bool = False,
) -> ClaimSlotBackfillStats:
    """计算或应用 slot、tags 和新 conflict_key 回填，默认不写数据库。"""
    columns = {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in connection.execute("PRAGMA table_info(claims)").fetchall()
    }
    required = {
        "namespace_key",
        "subject_entity_id",
        "predicate",
        "canonical_attribute",
        "canonical_slot",
        "topic_tags_json",
        "qualifiers_json",
        "conflict_key",
    }
    if missing := required - columns:
        raise sqlite3.OperationalError(f"claims table is missing Phase 17 columns: {sorted(missing)}")

    rows = connection.execute(
        "SELECT id,namespace_key,subject_entity_id,predicate,canonical_attribute,qualifiers_json "
        "FROM claims ORDER BY id"
    ).fetchall()
    operational = 0
    null_slot = 0
    tag_counts: Counter[str] = Counter()
    updates: list[tuple[str | None, str, str | None, str]] = []
    for row in rows:
        claim = (
            dict(row)
            if isinstance(row, sqlite3.Row)
            else dict(
                zip(
                    (
                        "id",
                        "namespace_key",
                        "subject_entity_id",
                        "predicate",
                        "canonical_attribute",
                        "qualifiers_json",
                    ),
                    row,
                    strict=True,
                )
            )
        )
        attribute = str(claim["canonical_attribute"] or "custom.unknown")
        slot = validate_canonical_slot(attribute)
        tags = _tags_for_attribute(attribute)
        qualifiers = _decode_qualifiers(str(claim["id"]), claim["qualifiers_json"])
        conflict_key = compute_conflict_key(
            str(claim["namespace_key"] or "default"),
            str(claim["subject_entity_id"] or ""),
            str(claim["predicate"] or ""),
            slot,
            qualifiers,
        )
        operational += slot is not None
        null_slot += slot is None
        tag_counts.update(tags)
        updates.append(
            (
                slot,
                json.dumps(tags, ensure_ascii=False, separators=(",", ":")),
                conflict_key,
                claim["id"],
            )
        )

    if apply:
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                "UPDATE claims SET canonical_slot=?,topic_tags_json=?,conflict_key=? WHERE id=?",
                updates,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    return ClaimSlotBackfillStats(
        total=len(rows),
        operational=operational,
        null_slot=null_slot,
        tag_counts=dict(sorted(tag_counts.items())),
        applied=apply,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """运行默认 dry-run 的命令行回填工具。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=default_database_path(), help="SQLite database path")
    parser.add_argument("--apply", action="store_true", help="apply updates; default is dry-run")
    args = parser.parse_args(argv)
    connection = sqlite3.connect(args.db)
    connection.row_factory = sqlite3.Row
    try:
        stats = backfill_claim_slots_v1(connection, apply=args.apply)
    finally:
        connection.close()
    print(json.dumps(stats.__dict__, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
