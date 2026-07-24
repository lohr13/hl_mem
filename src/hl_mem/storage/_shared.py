"""SQLite 仓储共享的边界转换和插入工具。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any


def decode_json(value: str | bytes | bytearray | None) -> Any:
    """在仓储边界将 JSON 存储值解码为 Python 值。"""
    return None if value is None else json.loads(value)


def encode_json(value: Any, *, sort_keys: bool = False) -> str:
    """在仓储边界将 Python 值编码为 JSON 存储值。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=sort_keys)


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """将可选 SQLite 行转换为普通字典。"""
    return dict(row) if row else None


def insert_row(connection: sqlite3.Connection, table: str, data: dict[str, Any], commit: bool = False) -> bool:
    """使用 INSERT OR IGNORE 写入一行并返回是否实际插入。"""
    columns = ", ".join(data)
    placeholders = ", ".join("?" for _ in data)
    before = connection.total_changes
    connection.execute(
        f"INSERT OR IGNORE INTO {table} ({columns}) VALUES ({placeholders})",
        tuple(data.values()),
    )
    if commit:
        connection.commit()
    return connection.total_changes > before


def sanitize_fts_query(query: str, *, tokenizer: str = "unicode61") -> str:
    """清洗 FTS5 查询字符串，安全引用用户文本为字面量 phrase。

    对两种 tokenizer 统一使用双引号包裹——这能安全转义所有
    FTS5 特殊字符（AND/OR/NOT/* /^/+/: 等），对 unicode61 和
    trigram 都有效。

    trigram 模式下，少于 3 个字符的 token 无法生成 trigram，
    需要在引用前过滤掉以避免空 phrase 报错。
    """
    tokens = query.strip().split()
    if not tokens:
        return '""'
    if tokenizer == "trigram":
        tokens = [t for t in tokens if len(t) >= 3]
        if not tokens:
            return '""'
    return " ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


def is_fts_syntax_error(error: sqlite3.OperationalError) -> bool:
    """仅识别由用户 MATCH 表达式触发的 FTS 语法错误。"""
    message = str(error).lower()
    return any(marker in message for marker in ("fts5: syntax error", "malformed match", "unterminated string"))
