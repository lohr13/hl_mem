from __future__ import annotations

from pathlib import Path

import pytest

from hl_mem.domain.governance import DecisionEnvelope, UnsafeGovernanceSnapshot
from hl_mem.storage.database import Database
from hl_mem.storage.governance import GovernanceActionRepository, StaleGovernanceAction

NOW = "2026-08-25T08:00:00+00:00"


def _decision() -> DecisionEnvelope:
    return DecisionEnvelope(
        domain="conflict",
        subject_ref="case-1",
        input_fingerprint="f" * 64,
        policy_version="conflict-auto-v1",
        tier="L0",
        decision="keep_left",
        confidence=1.0,
        resolution_rule="strict_authority",
        resolver_model=None,
        evidence_ids=("evidence-1",),
    )


def test_observe_is_idempotent_by_domain_subject_fingerprint_and_policy(tmp_path: Path) -> None:
    connection = Database(tmp_path / "observe.db").open()
    repository = GovernanceActionRepository(connection)

    first = repository.record(
        _decision(),
        before={"case": {"status": "manual_required"}},
        after={"case": {"status": "resolved"}},
        status="observed",
        created_at=NOW,
    )
    second = repository.record(
        _decision(),
        before={"case": {"status": "manual_required"}},
        after={"case": {"status": "resolved"}},
        status="observed",
        created_at="2026-08-25T09:00:00+00:00",
    )

    assert second["id"] == first["id"]
    assert connection.execute("SELECT count(*) FROM governance_actions").fetchone()[0] == 1
    assert first["evidence_ids"] == ["evidence-1"]


def test_unique_identity_rejects_a_different_decision_for_same_input(tmp_path: Path) -> None:
    connection = Database(tmp_path / "unique.db").open()
    repository = GovernanceActionRepository(connection)
    repository.record(
        _decision(),
        before={"case": {"status": "manual_required"}},
        after={"case": {"status": "resolved"}},
        status="observed",
        created_at=NOW,
    )
    changed = DecisionEnvelope(**{**_decision().__dict__, "decision": "keep_right"})

    with pytest.raises(StaleGovernanceAction, match="different decision"):
        repository.record(
            changed,
            before={"case": {"status": "manual_required"}},
            after={"case": {"status": "resolved"}},
            status="observed",
            created_at=NOW,
        )


def test_rollback_requires_current_state_to_match_after_snapshot(tmp_path: Path) -> None:
    connection = Database(tmp_path / "rollback.db").open()
    repository = GovernanceActionRepository(connection)
    action = repository.record(
        _decision(),
        before={"case": {"status": "manual_required", "revision": 4}},
        after={"case": {"status": "resolved", "revision": 4}},
        status="applied",
        created_at=NOW,
        applied_at=NOW,
    )

    with pytest.raises(StaleGovernanceAction, match="after snapshot"):
        repository.mark_rolled_back(
            action["id"],
            current={"case": {"status": "resolved", "revision": 5}},
            reason="operator request",
            rolled_back_at="2026-08-25T09:00:00+00:00",
        )

    before = repository.mark_rolled_back(
        action["id"],
        current={"case": {"status": "resolved", "revision": 4}},
        reason="operator request",
        rolled_back_at="2026-08-25T09:00:00+00:00",
    )
    assert before == {"case": {"revision": 4, "status": "manual_required"}}
    stored = connection.execute(
        "SELECT status,rollback_reason FROM governance_actions WHERE id=?", (action["id"],)
    ).fetchone()
    assert tuple(stored) == ("rolled_back", "operator request")


def test_snapshot_rejects_hidden_reasoning_fields(tmp_path: Path) -> None:
    connection = Database(tmp_path / "unsafe.db").open()

    with pytest.raises(UnsafeGovernanceSnapshot, match="chain_of_thought"):
        GovernanceActionRepository(connection).record(
            _decision(),
            before={"case": {"status": "manual_required"}},
            after={"chain_of_thought": "hidden"},
            status="observed",
            created_at=NOW,
        )
