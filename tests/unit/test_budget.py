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
