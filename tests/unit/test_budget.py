from datetime import date

from hl_mem.ingest.budget import TokenBudget


class _TrackingConnection:
    def __init__(self) -> None:
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, *_args):
        return self

    def fetchone(self):
        return None

    def close(self) -> None:
        self.closed = True


def test_constructor_and_stats_close_short_lived_connections(tmp_path, monkeypatch) -> None:
    connections = []

    def connect(_budget):
        connection = _TrackingConnection()
        connections.append(connection)
        return connection

    monkeypatch.setattr(TokenBudget, "_connect", connect)

    budget = TokenBudget(10, tmp_path / "budget.db")
    assert len(connections) == 1
    assert connections[0].closed is True

    assert budget.get_stats()["used_tokens"] == 0
    assert len(connections) == 2
    assert connections[1].closed is True


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
