from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from hl_mem.application.ingest import IngestService
from hl_mem.application.plan_fulfillment import PlanFulfillmentService
from hl_mem.domain.relations import EXPANDABLE_RELATION_TYPES, RelationType
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.storage.entities import EntityRepository
from hl_mem.storage.governance import StaleGovernanceAction
from hl_mem.storage.jobs import JobRepository
from hl_mem.workers.plan_fulfillment import enqueue_plan_reconciliation_scan

TARGET = "instrument:CN:SH:600519"


def test_plan_relations_are_not_recall_expansion_edges() -> None:
    assert RelationType.FULFILLED_BY not in EXPANDABLE_RELATION_TYPES


def _connection(tmp_path: Path, mode: str = "enforce"):
    settings = replace(
        Settings.for_test(),
        database_path=str(tmp_path / f"plan-{mode}.db"),
        price_target_mode="enforce",
        plan_fulfillment_mode=mode,
    )
    connection = Database(settings=settings).open()
    entities = EntityRepository(connection)
    entities.create_entity(
        TARGET,
        "instrument",
        "CN:SH:600519",
        "Kweichow Moutai",
        now="2026-08-25T08:00:00+00:00",
    )
    entities.create_alias(
        "600519.SH",
        "instrument",
        TARGET,
        "config_explicit",
        valid_from="2026-08-25T08:00:00+00:00",
    )
    connection.commit()
    return connection


def _store(
    connection,
    *,
    suffix: str,
    occurred_at: str,
    phase: str,
    amount: str,
    family: str = "open",
    account: str | None = None,
    subject: str = "user",
) -> str:
    qualifiers = {
        "action_family": family,
        "assertion_phase": phase,
        "direction": "long" if family in {"open", "increase"} else "out",
        "quantity_mode": "exact",
        "quantity": amount,
        "quantity_unit": "share",
        "account": account,
    }
    is_plan = phase == "plan"
    verb = {
        "plan": "plan to buy",
        "execution": "bought",
        "cancellation": "cancel buying",
        "replacement": "replace buying plan",
    }[phase]
    stored = IngestService.store_extracted(
        connection,
        ExtractedClaim(
            predicate="plan" if is_plan else "fact",
            value=f"{verb} {amount} shares of Kweichow Moutai 600519.SH",
            subject=subject,
            qualifiers=qualifiers,
            canonical_attribute="plan.other" if is_plan else "fact.other",
            scope="temporal" if is_plan else "permanent",
            importance=0.9,
            assertion_kind="unknown" if is_plan else "observation",
        ),
        {
            "id": f"event-{suffix}",
            "tenant_id": "default",
            "actor_type": "user",
            "occurred_at": occurred_at,
        },
        occurred_at,
        FakeEmbedder(8),
    )
    return str(stored.claim_id)


@pytest.mark.parametrize(
    ("phase", "outcome_type", "relation"),
    [
        ("execution", "complete", "fulfilled_by"),
        ("cancellation", "cancel", "cancelled_by"),
        ("replacement", "replace", "replaced_by"),
    ],
)
def test_terminal_outcomes_close_only_plan_valid_time(
    tmp_path: Path,
    phase: str,
    outcome_type: str,
    relation: str,
) -> None:
    connection = _connection(tmp_path)
    plan_id = _store(
        connection,
        suffix=f"plan-{phase}",
        occurred_at="2026-08-25T09:00:00+00:00",
        phase="plan",
        amount="100",
    )
    before = ClaimRepository(connection).get_claim(plan_id)
    result_id = _store(
        connection,
        suffix=f"result-{phase}",
        occurred_at="2026-08-25T10:00:00+00:00",
        phase=phase,
        amount="100",
    )

    result = PlanFulfillmentService(connection, mode="enforce").reconcile(result_id, now="2026-08-25T10:01:00+00:00")
    after = ClaimRepository(connection).get_claim(plan_id)

    assert (result["status"], result["outcome_type"]) == ("applied", outcome_type)
    assert after["valid_to"] == "2026-08-25T10:00:00+00:00"
    assert (after["status"], after["recorded_to"], after["superseded_by_id"]) == (
        before["status"],
        before["recorded_to"],
        before["superseded_by_id"],
    )
    relation_row = connection.execute(
        "SELECT provenance,proposal_id FROM memory_relations "
        "WHERE from_id=? AND to_id=? AND relation=? AND valid_to IS NULL",
        (plan_id, result_id, relation),
    ).fetchone()
    assert tuple(relation_row) == ("deterministic", None)


def test_cancellation_may_omit_quantity_only_with_strong_plan_id(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    plan_id = _store(
        connection,
        suffix="anchored-plan",
        occurred_at="2026-08-25T09:00:00+00:00",
        phase="plan",
        amount="100",
    )
    result_id = _store(
        connection,
        suffix="anchored-cancel",
        occurred_at="2026-08-25T10:00:00+00:00",
        phase="cancellation",
        amount="100",
    )
    result = ClaimRepository(connection).get_claim(result_id)
    qualifiers = dict(result["qualifiers"])
    qualifiers.update(quantity_mode="unknown", plan_claim_id=plan_id)
    qualifiers.pop("quantity", None)
    qualifiers.pop("quantity_unit", None)
    connection.execute(
        "UPDATE claims SET qualifiers_json=? WHERE id=?",
        (json.dumps(qualifiers, sort_keys=True), result_id),
    )
    connection.commit()

    outcome = PlanFulfillmentService(connection, mode="enforce").reconcile(result_id, now="2026-08-25T10:01:00+00:00")

    assert (outcome["status"], outcome["outcome_type"]) == ("applied", "cancel")


def test_partial_recomputes_decimal_total_and_closes_at_latest_result(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    plan_id = _store(
        connection,
        suffix="partial-plan",
        occurred_at="2026-08-25T09:00:00+00:00",
        phase="plan",
        amount="100.00",
    )
    later_id = _store(
        connection,
        suffix="partial-60",
        occurred_at="2026-08-25T10:10:00+00:00",
        phase="execution",
        amount="60.0",
    )
    earlier_id = _store(
        connection,
        suffix="partial-40",
        occurred_at="2026-08-25T10:05:00+00:00",
        phase="execution",
        amount="40.00",
    )
    service = PlanFulfillmentService(connection, mode="enforce")

    first = service.reconcile(later_id, now="2026-08-25T10:11:00+00:00")
    second = service.reconcile(earlier_id, now="2026-08-25T10:12:00+00:00")

    assert (first["outcome_type"], first["cumulative_quantity_text"]) == ("partial", "60")
    assert (second["outcome_type"], second["cumulative_quantity_text"]) == ("partial", "100")
    assert ClaimRepository(connection).get_claim(plan_id)["valid_to"] == "2026-08-25T10:10:00+00:00"
    assert (
        connection.execute(
            "SELECT count(*) FROM plan_outcomes WHERE plan_claim_id=? AND status='applied'",
            (plan_id,),
        ).fetchone()[0]
        == 3
    )


def test_overfill_is_ambiguous_and_replay_is_idempotent(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    plan_id = _store(
        connection,
        suffix="overfill-plan",
        occurred_at="2026-08-25T09:00:00+00:00",
        phase="plan",
        amount="100",
    )
    result_id = _store(
        connection,
        suffix="overfill-result",
        occurred_at="2026-08-25T10:00:00+00:00",
        phase="execution",
        amount="110",
    )
    service = PlanFulfillmentService(connection, mode="enforce")

    first = service.reconcile(result_id, now="2026-08-25T10:01:00+00:00")
    second = service.reconcile(result_id, now="2026-08-25T10:02:00+00:00")

    assert first["status"] == "ambiguous"
    assert second == first
    assert ClaimRepository(connection).get_claim(plan_id)["valid_to"] is None


def test_multiple_non_equivalent_logical_plans_are_ambiguous(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    plan_ids = [
        _store(
            connection,
            suffix=f"group-{subject}",
            occurred_at="2026-08-25T09:00:00+00:00",
            phase="plan",
            amount="100",
            subject=subject,
        )
        for subject in ("user", "backup-plan-owner")
    ]
    result_id = _store(
        connection,
        suffix="ambiguous-result",
        occurred_at="2026-08-25T10:00:00+00:00",
        phase="execution",
        amount="100",
    )

    result = PlanFulfillmentService(connection, mode="enforce").reconcile(result_id, now="2026-08-25T10:01:00+00:00")

    assert result["reason"] == "ambiguous_multiple_groups"
    assert all(ClaimRepository(connection).get_claim(item)["valid_to"] is None for item in plan_ids)


def test_observe_records_would_apply_without_closing_plan(tmp_path: Path) -> None:
    connection = _connection(tmp_path, "observe")
    plan_id = _store(
        connection,
        suffix="observe-plan",
        occurred_at="2026-08-25T09:00:00+00:00",
        phase="plan",
        amount="100",
    )
    result_id = _store(
        connection,
        suffix="observe-result",
        occurred_at="2026-08-25T10:00:00+00:00",
        phase="execution",
        amount="100",
    )

    result = PlanFulfillmentService(connection, mode="observe").reconcile(result_id, now="2026-08-25T10:01:00+00:00")

    assert result["status"] == "observed"
    assert ClaimRepository(connection).get_claim(plan_id)["valid_to"] is None
    assert (
        connection.execute(
            "SELECT status FROM governance_actions WHERE domain='plan' AND subject_ref=?",
            (plan_id,),
        ).fetchone()[0]
        == "observed"
    )

    promoted = PlanFulfillmentService(connection, mode="enforce").reconcile(result_id, now="2026-08-25T10:02:00+00:00")
    assert promoted["status"] == "applied"
    assert ClaimRepository(connection).get_claim(plan_id)["valid_to"] == "2026-08-25T10:00:00+00:00"


def test_applied_closure_can_roll_back_with_matching_after_fingerprint(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    plan_id = _store(
        connection,
        suffix="rollback-plan",
        occurred_at="2026-08-25T09:00:00+00:00",
        phase="plan",
        amount="100",
    )
    result_id = _store(
        connection,
        suffix="rollback-result",
        occurred_at="2026-08-25T10:00:00+00:00",
        phase="execution",
        amount="100",
    )
    service = PlanFulfillmentService(connection, mode="enforce")
    applied = service.reconcile(result_id, now="2026-08-25T10:01:00+00:00")

    rolled_back = service.rollback(
        applied["action_id"],
        now="2026-08-25T10:02:00+00:00",
        reason="operator correction",
    )

    assert rolled_back["status"] == "rolled_back"
    assert ClaimRepository(connection).get_claim(plan_id)["valid_to"] is None
    assert (
        connection.execute(
            "SELECT count(*) FROM plan_outcomes WHERE plan_claim_id=? AND status='rolled_back'",
            (plan_id,),
        ).fetchone()[0]
        == 1
    )
    assert (
        connection.execute(
            "SELECT count(*) FROM memory_relations WHERE from_id=? AND valid_to IS NOT NULL",
            (plan_id,),
        ).fetchone()[0]
        == 1
    )


def test_result_ingest_queues_reconciliation_in_the_claim_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = _connection(tmp_path)
    _store(
        connection,
        suffix="queue-plan",
        occurred_at="2026-08-25T09:00:00+00:00",
        phase="plan",
        amount="100",
    )
    original = JobRepository.insert_job
    before_count = connection.execute("SELECT count(*) FROM claims").fetchone()[0]

    def fail_reconciliation(self, job, commit=True):
        if job["job_type"] == "reconcile_plan_result":
            raise RuntimeError("queue unavailable")
        return original(self, job, commit=commit)

    monkeypatch.setattr(JobRepository, "insert_job", fail_reconciliation)
    with pytest.raises(RuntimeError, match="queue unavailable"):
        _store(
            connection,
            suffix="queue-result",
            occurred_at="2026-08-25T10:00:00+00:00",
            phase="execution",
            amount="100",
        )

    assert connection.execute("SELECT count(*) FROM claims").fetchone()[0] == before_count


def test_bounded_maintenance_scan_does_not_requeue_same_fingerprint(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    _store(
        connection,
        suffix="scan-plan",
        occurred_at="2026-08-25T09:00:00+00:00",
        phase="plan",
        amount="100",
    )
    result_id = _store(
        connection,
        suffix="scan-result",
        occurred_at="2026-08-25T10:00:00+00:00",
        phase="execution",
        amount="100",
    )
    connection.execute("DELETE FROM jobs WHERE job_type='reconcile_plan_result'")
    connection.commit()

    first = enqueue_plan_reconciliation_scan(connection, "2026-08-25T10:01:00+00:00", mode="audit", limit=1)
    second = enqueue_plan_reconciliation_scan(connection, "2026-08-25T10:02:00+00:00", mode="audit", limit=1)
    promoted = enqueue_plan_reconciliation_scan(connection, "2026-08-25T10:03:00+00:00", mode="enforce", limit=1)

    assert (first["scanned"], first["enqueued"]) == (1, 1)
    assert (second["scanned"], second["enqueued"]) == (1, 0)
    assert (promoted["scanned"], promoted["enqueued"]) == (1, 1)
    payloads = connection.execute("SELECT payload_json FROM jobs WHERE job_type='reconcile_plan_result'").fetchall()
    assert all(result_id in row[0] for row in payloads)


def test_rollback_fails_closed_after_plan_state_changes(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    plan_id = _store(
        connection,
        suffix="stale-plan",
        occurred_at="2026-08-25T09:00:00+00:00",
        phase="plan",
        amount="100",
    )
    result_id = _store(
        connection,
        suffix="stale-result",
        occurred_at="2026-08-25T10:00:00+00:00",
        phase="execution",
        amount="100",
    )
    service = PlanFulfillmentService(connection, mode="enforce")
    applied = service.reconcile(result_id, now="2026-08-25T10:01:00+00:00")
    connection.execute("UPDATE claims SET valid_to='2026-08-25T10:00:01+00:00' WHERE id=?", (plan_id,))
    connection.commit()

    with pytest.raises(StaleGovernanceAction):
        service.rollback(applied["action_id"], now="2026-08-25T10:02:00+00:00", reason="stale attempt")

    assert (
        connection.execute("SELECT status FROM governance_actions WHERE id=?", (applied["action_id"],)).fetchone()[0]
        == "applied"
    )
