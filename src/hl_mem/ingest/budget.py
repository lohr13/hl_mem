from __future__ import annotations

import json
import threading
from datetime import date
from pathlib import Path
from typing import Callable


class TokenBudget:
    """A small, process-safe-enough daily token ledger persisted as JSON."""

    def __init__(
        self,
        daily_limit: int = 500_000,
        path: str | Path = "hl_mem_budget.json",
        today: Callable[[], date] = date.today,
    ) -> None:
        self.daily_limit = daily_limit
        self.path = Path(path)
        self._today = today
        self._lock = threading.Lock()
        self._state = self._load()
        self._reset_if_needed()

    def can_spend(self, estimated_tokens: int) -> bool:
        if estimated_tokens < 0:
            raise ValueError("estimated_tokens must be non-negative")
        with self._lock:
            self._reset_if_needed()
            return self._state["used_tokens"] + estimated_tokens <= self.daily_limit

    def record_usage(self, actual_tokens: int) -> None:
        if actual_tokens < 0:
            raise ValueError("actual_tokens must be non-negative")
        with self._lock:
            self._reset_if_needed()
            self._state["used_tokens"] += actual_tokens
            self._save()

    def get_stats(self) -> dict[str, int | str]:
        with self._lock:
            self._reset_if_needed()
            used = self._state["used_tokens"]
            return {
                "date": self._state["date"],
                "daily_limit": self.daily_limit,
                "used_tokens": used,
                "remaining_tokens": max(0, self.daily_limit - used),
            }

    def _load(self) -> dict[str, int | str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return {"date": str(data["date"]), "used_tokens": int(data["used_tokens"])}
        except (OSError, ValueError, KeyError, TypeError):
            return {"date": self._today().isoformat(), "used_tokens": 0}

    def _reset_if_needed(self) -> None:
        current = self._today().isoformat()
        if self._state["date"] != current:
            self._state = {"date": current, "used_tokens": 0}
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self._state), encoding="utf-8")
        temporary.replace(self.path)
