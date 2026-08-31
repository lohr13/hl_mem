from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest

from hl_mem.errors import UsageLimitExceededError, UsageReservationError
from hl_mem.observability.usage import (
    UsageAmount,
    UsageGovernor,
    UsageIdentity,
    UsageLimits,
    UsageReservation,
    default_usage_ledger_path,
)
from hl_mem.plugins.contracts import ProviderCapability

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
IDENTITY = UsageIdentity(ProviderCapability.LLM, "extract", "hl-mem.builtin", "dashscope", "qwen")


class Clock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _governor(
    path: Path,
    *,
    limits: UsageLimits | None = None,
    clock: Clock | None = None,
    lease_seconds: int = 60,
) -> UsageGovernor:
    return UsageGovernor(
        path,
        limits or UsageLimits(0, 0, 0),
        lease_seconds=lease_seconds,
        now=clock or Clock(),
    )


def test_usage_amount_is_frozen_non_negative_addable_and_scalable() -> None:
    first = UsageAmount(requests=1, input_tokens=3, cost_microunits=7)
    second = UsageAmount(output_tokens=2, embedding_items=4, cost_microunits=5)

    assert first + second == UsageAmount(
        requests=1,
        input_tokens=3,
        output_tokens=2,
        embedding_items=4,
        cost_microunits=12,
    )
    assert first.scale(2) == UsageAmount(requests=2, input_tokens=6, cost_microunits=14)
    assert first.total_tokens == 3
    assert (first + UsageAmount(cost_microunits=None)).cost_microunits is None
    with pytest.raises(ValueError, match="non-negative"):
        UsageAmount(requests=-1)
    with pytest.raises(FrozenInstanceError):
        first.requests = 2  # type: ignore[misc]


def test_default_path_reuses_the_existing_budget_sidecar_name(tmp_path: Path) -> None:
    assert default_usage_ledger_path(tmp_path / "hl_mem.db") == tmp_path / "hl_mem.budget.db"


def test_usage_identity_rejects_payload_like_high_cardinality_labels() -> None:
    with pytest.raises(ValueError, match="operation"):
        UsageIdentity(ProviderCapability.LLM, "private prompt text", "hl-mem.builtin", "dashscope", "qwen")
    with pytest.raises(ValueError, match="model"):
        UsageIdentity(ProviderCapability.LLM, "extract", "hl-mem.builtin", "dashscope", "secret model value")


def test_reserve_attempt_settle_and_snapshot_are_exact(tmp_path: Path) -> None:
    governor = _governor(tmp_path / "usage.db", limits=UsageLimits(10, 100, 1_000))
    reservation = governor.reserve(
        IDENTITY,
        UsageAmount(requests=1, input_tokens=5, cost_microunits=20),
    )

    assert isinstance(reservation, UsageReservation)
    assert governor.mark_attempt(reservation.id) == 1
    governor.settle(
        reservation.id,
        UsageAmount(requests=1, input_tokens=4, output_tokens=3, cost_microunits=18),
        status="success",
        latency_ms=12.5,
    )

    snapshot = governor.snapshot()
    assert snapshot["settled"] == {
        "requests": 1,
        "input_tokens": 4,
        "output_tokens": 3,
        "total_tokens": 7,
        "embedding_items": 0,
        "rerank_documents": 0,
        "images": 0,
        "cost_microunits": 18,
    }
    assert snapshot["reserved"]["requests"] == 0
    assert snapshot["remaining"] == {"requests": 9, "tokens": 93, "cost_microunits": 982}
    assert snapshot["counts_by_capability"] == {"llm": 1}
    assert snapshot["unknown_cost_count"] == 0


def test_concurrent_reservations_cannot_both_spend_the_last_tokens(tmp_path: Path) -> None:
    path = tmp_path / "usage.db"
    governors = [
        _governor(path, limits=UsageLimits(0, 10, 0)),
        _governor(path, limits=UsageLimits(0, 10, 0)),
    ]
    barrier = Barrier(2)

    def reserve(governor: UsageGovernor) -> UsageReservation | Exception:
        barrier.wait()
        try:
            return governor.reserve(IDENTITY, UsageAmount(requests=1, input_tokens=7))
        except Exception as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(reserve, governors))

    assert sum(isinstance(item, UsageReservation) for item in outcomes) == 1
    assert sum(isinstance(item, UsageLimitExceededError) for item in outcomes) == 1


def test_actual_usage_may_exceed_reservation_and_is_not_hidden(tmp_path: Path) -> None:
    governor = _governor(tmp_path / "usage.db", limits=UsageLimits(0, 10, 0))
    reservation = governor.reserve(IDENTITY, UsageAmount(input_tokens=5))
    governor.mark_attempt(reservation.id)
    governor.settle(reservation.id, UsageAmount(input_tokens=12), status="success", latency_ms=1.0)

    assert governor.snapshot()["settled"]["total_tokens"] == 12
    assert governor.snapshot()["remaining"]["tokens"] == 0


def test_same_finalization_is_idempotent_but_contradictions_fail(tmp_path: Path) -> None:
    governor = _governor(tmp_path / "usage.db")
    reservation = governor.reserve(IDENTITY, UsageAmount(requests=1))
    actual = UsageAmount(requests=1, input_tokens=2)
    governor.settle(reservation.id, actual, status="success", latency_ms=1.0)
    governor.settle(reservation.id, actual, status="success", latency_ms=1.0)

    with pytest.raises(UsageReservationError, match="contradictory"):
        governor.release(reservation.id, reason="late_cancellation")
    with pytest.raises(UsageReservationError, match="contradictory"):
        governor.settle(reservation.id, UsageAmount(requests=2), status="success", latency_ms=1.0)


def test_expired_unsent_is_released_and_sent_is_settled_unknown(tmp_path: Path) -> None:
    clock = Clock()
    governor = _governor(tmp_path / "usage.db", clock=clock, lease_seconds=10)
    unsent = governor.reserve(IDENTITY, UsageAmount(requests=1, input_tokens=2))
    sent = governor.reserve(IDENTITY, UsageAmount(requests=1, input_tokens=5))
    assert unsent.id != sent.id
    governor.mark_attempt(sent.id)
    clock.value += timedelta(seconds=11)

    assert governor.recover_expired() == {"released": 1, "settled_unknown": 1}
    snapshot = governor.snapshot()
    assert snapshot["settled"]["requests"] == 1
    assert snapshot["settled"]["input_tokens"] == 5
    assert snapshot["reserved"]["requests"] == 0


def test_release_after_a_sent_attempt_is_rejected(tmp_path: Path) -> None:
    governor = _governor(tmp_path / "usage.db")
    reservation = governor.reserve(IDENTITY, UsageAmount(requests=1))
    governor.mark_attempt(reservation.id)

    with pytest.raises(UsageReservationError, match="attempt"):
        governor.release(reservation.id, reason="unsafe")


def test_natural_utc_day_resets_limits_without_erasing_history(tmp_path: Path) -> None:
    clock = Clock()
    governor = _governor(tmp_path / "usage.db", limits=UsageLimits(0, 5, 0), clock=clock)
    reservation = governor.reserve(IDENTITY, UsageAmount(input_tokens=5))
    governor.settle(reservation.id, UsageAmount(input_tokens=5), status="success", latency_ms=1.0)
    with pytest.raises(UsageLimitExceededError):
        governor.reserve(IDENTITY, UsageAmount(input_tokens=1))

    first_day = clock.value.date().isoformat()
    clock.value += timedelta(days=1)
    assert governor.snapshot()["settled"]["total_tokens"] == 0
    assert governor.reserve(IDENTITY, UsageAmount(input_tokens=5))
    assert governor.snapshot(day=first_day)["settled"]["total_tokens"] == 5


def test_non_positive_limits_are_unlimited(tmp_path: Path) -> None:
    governor = _governor(tmp_path / "usage.db", limits=UsageLimits(-1, 0, -10))

    reservation = governor.reserve(
        IDENTITY,
        UsageAmount(requests=999, input_tokens=999, cost_microunits=None),
    )
    governor.settle_unknown(reservation.id, status="unknown", latency_ms=1.0, error_class="Timeout")

    snapshot = governor.snapshot()
    assert snapshot["remaining"] == {"requests": -1, "tokens": -1, "cost_microunits": -1}
    assert snapshot["unknown_cost_count"] == 1


def test_finite_cost_limit_rejects_unknown_estimate(tmp_path: Path) -> None:
    governor = _governor(tmp_path / "usage.db", limits=UsageLimits(0, 0, 100))

    with pytest.raises(UsageLimitExceededError, match="cost"):
        governor.reserve(IDENTITY, UsageAmount(requests=1, cost_microunits=None))


def test_unknown_actual_cost_only_reserves_cost_for_attempts_that_ran(tmp_path: Path) -> None:
    path = tmp_path / "usage.db"
    governor = _governor(path)
    reservation = governor.reserve(IDENTITY, UsageAmount(requests=3, cost_microunits=6))
    governor.mark_attempt(reservation.id)
    governor.mark_attempt(reservation.id)
    governor.settle(
        reservation.id,
        UsageAmount(requests=2, cost_microunits=None),
        status="success",
        latency_ms=1.0,
    )

    with sqlite3.connect(path) as connection:
        cost, unknown = connection.execute(
            "SELECT cost_microunits,unknown_cost FROM usage_events WHERE reservation_id=?",
            (reservation.id,),
        ).fetchone()
    assert (cost, unknown) == (4, 1)


def test_request_and_known_cost_limits_include_active_reservations(tmp_path: Path) -> None:
    governor = _governor(tmp_path / "usage.db", limits=UsageLimits(1, 0, 10))
    governor.reserve(IDENTITY, UsageAmount(requests=1, cost_microunits=7))

    with pytest.raises(UsageLimitExceededError, match="request"):
        governor.reserve(IDENTITY, UsageAmount(requests=1, cost_microunits=0))

    cost_only = UsageIdentity(ProviderCapability.LLM, "verify", "hl-mem.builtin", "dashscope", "qwen")
    with pytest.raises(UsageLimitExceededError, match="cost"):
        governor.reserve(cost_only, UsageAmount(cost_microunits=4))


def test_legacy_token_budget_is_imported_exactly_once(tmp_path: Path) -> None:
    path = tmp_path / "usage.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE token_budget (budget_date TEXT PRIMARY KEY, used_tokens INTEGER NOT NULL)")
        connection.execute("INSERT INTO token_budget VALUES (?, ?)", (NOW.date().isoformat(), 7))

    first = _governor(path)
    second = _governor(path)

    assert first.snapshot()["settled"]["total_tokens"] == 7
    assert second.snapshot()["settled"]["total_tokens"] == 7


def test_unknown_reservation_and_finalization_ids_fail_cleanly(tmp_path: Path) -> None:
    governor = _governor(tmp_path / "usage.db")

    with pytest.raises(UsageReservationError, match="unknown"):
        governor.mark_attempt("missing")
    with pytest.raises(UsageReservationError, match="unknown"):
        governor.settle("missing", UsageAmount(), status="success", latency_ms=0)
