from datetime import date

from hl_mem.ingest.budget import TokenBudget


def test_budget_records_persists_and_exhausts(tmp_path) -> None:
    path = tmp_path / "budget.json"
    budget = TokenBudget(10, path)
    assert budget.can_spend(10)
    budget.record_usage(7)
    assert budget.can_spend(3)
    assert not budget.can_spend(4)
    assert TokenBudget(10, path).get_stats()["used_tokens"] == 7


def test_budget_resets_on_natural_day(tmp_path) -> None:
    current = [date(2026, 7, 20)]
    budget = TokenBudget(10, tmp_path / "budget.json", today=lambda: current[0])
    budget.record_usage(10)
    current[0] = date(2026, 7, 21)
    assert budget.can_spend(10)
    assert budget.get_stats()["used_tokens"] == 0


def test_unlimited_budget_never_exhausts(tmp_path) -> None:
    budget = TokenBudget(0, tmp_path / "budget.json")
    budget.record_usage(999_999_999)
    assert budget.can_spend(999_999_999)
    stats = budget.get_stats()
    assert stats["remaining_tokens"] == -1


def test_budget_facade_uses_the_versioned_usage_schema(tmp_path) -> None:
    path = tmp_path / "budget.db"
    TokenBudget(10, path).record_usage(3)

    import sqlite3

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"usage_reservations", "usage_events"} <= tables
