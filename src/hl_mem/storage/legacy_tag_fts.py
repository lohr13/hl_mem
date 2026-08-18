"""兼容 legacy tag FTS 投影的 Claim 标签更新。"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence


def execute_claim_tags_update(
    connection: sqlite3.Connection,
    claim_id: str,
    statement: str,
    parameters: Sequence[object],
) -> sqlite3.Cursor:
    """执行标签更新，并仅对 legacy FTS SQL logic error 做事务内恢复。"""
    try:
        return connection.execute(statement, parameters)
    except sqlite3.OperationalError as error:
        if str(error) != "SQL logic error":
            raise
        return _execute_without_legacy_tag_trigger(
            connection,
            claim_id,
            statement,
            parameters,
            error,
        )


def _execute_without_legacy_tag_trigger(
    connection: sqlite3.Connection,
    claim_id: str,
    statement: str,
    parameters: Sequence[object],
    original_error: sqlite3.OperationalError,
) -> sqlite3.Cursor:
    trigger = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='claims_tags_au'"
    ).fetchone()
    trigger_sql = str(trigger[0] if trigger is not None else "")
    if "claims_tags_fts" not in trigger_sql:
        raise original_error
    claim = connection.execute(
        "SELECT rowid,topic_tags_json FROM claims WHERE id=?",
        (claim_id,),
    ).fetchone()
    if claim is None:
        raise original_error

    connection.execute("DROP TRIGGER claims_tags_au")
    try:
        if not _delete_legacy_tag_projection(
            connection,
            int(claim[0]),
            str(claim[1] or ""),
        ):
            raise original_error
        cursor = connection.execute(statement, parameters)
        updated = connection.execute(
            "SELECT rowid,topic_tags_json FROM claims WHERE id=?",
            (claim_id,),
        ).fetchone()
        if updated is not None:
            connection.execute(
                "INSERT INTO claims_tags_fts(rowid,tags_text) VALUES(?,?)",
                (int(updated[0]), str(updated[1] or "")),
            )
        return cursor
    finally:
        connection.execute(trigger_sql)


def _delete_legacy_tag_projection(
    connection: sqlite3.Connection,
    rowid: int,
    tags_text: str,
) -> bool:
    statements = (
        (
            "INSERT INTO claims_tags_fts(claims_tags_fts,rowid,tags_text) " "VALUES('delete',?,?)",
            (rowid, tags_text),
        ),
        ("DELETE FROM claims_tags_fts WHERE rowid=?", (rowid,)),
    )
    for index, (statement, parameters) in enumerate(statements):
        savepoint = f"update_legacy_tag_projection_{index}"
        connection.execute(f"SAVEPOINT {savepoint}")
        try:
            connection.execute(statement, parameters)
        except sqlite3.OperationalError:
            connection.execute(f"ROLLBACK TO {savepoint}")
            connection.execute(f"RELEASE {savepoint}")
            continue
        connection.execute(f"RELEASE {savepoint}")
        return True
    return False
