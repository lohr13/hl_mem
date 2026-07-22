from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Callable


class TokenBudget:
    """使用 SQLite 事务维护单机多进程安全的每日 token 预算。"""

    def __init__(
        self,
        daily_limit: int = 500_000,
        path: str | Path = "hl_mem_budget.db",
        today: Callable[[], date] = date.today,
    ) -> None:
        self.daily_limit = daily_limit
        self.path = Path(path)
        self._today = today
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS token_budget ("
                "budget_date TEXT PRIMARY KEY, used_tokens INTEGER NOT NULL CHECK (used_tokens >= 0))"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def can_spend(self, estimated_tokens: int) -> bool:
        if estimated_tokens < 0:
            raise ValueError("estimated_tokens must be non-negative")
        return int(self.get_stats()["used_tokens"]) + estimated_tokens <= self.daily_limit

    def record_usage(self, actual_tokens: int) -> None:
        if actual_tokens < 0:
            raise ValueError("actual_tokens must be non-negative")
        current = self._today().isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO token_budget(budget_date,used_tokens) VALUES (?,?) "
                "ON CONFLICT(budget_date) DO UPDATE SET used_tokens=used_tokens+excluded.used_tokens",
                (current, actual_tokens),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_stats(self) -> dict[str, int | str]:
        current = self._today().isoformat()
        with self._connect() as connection:
            row = connection.execute("SELECT used_tokens FROM token_budget WHERE budget_date=?", (current,)).fetchone()
        used = int(row[0]) if row else 0
        return {
            "date": current,
            "daily_limit": self.daily_limit,
            "used_tokens": used,
            "remaining_tokens": max(0, self.daily_limit - used),
        }
