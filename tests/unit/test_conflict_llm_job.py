from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from hl_mem.application.conflicts import StaleConflictDecision
from hl_mem.errors import ConfigurationError
from hl_mem.evaluation.local_qwen_runner import OversizedDocket
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.workers.auto_resolve_conflicts import AutoDecision, L1Policy, auto_resolve_conflicts
from hl_mem.workers.conflict_judge import LocalConflictJudge, run_conflict_llm_job
from hl_mem.workers.job_handlers import JOB_HANDLERS

NOW = "2026-08-25T08:00:00+00:00"


def _manual_pair(connection, *, left_authority: str = "medium", right_authority: str = "medium") -> None:
    repository = ClaimRepository(connection)
    for claim_id, value, authority in (
        ("left", "8080", left_authority),
        ("right", "8081", right_authority),
    ):
        assert repository.insert_claim(
            {
                "id": claim_id,
                "namespace_key": "default",
                "subject_entity_id": "gateway",
                "predicate": "配置",
                "value": value,
                "qualifiers": {"service": "gateway"},
                "canonical_attribute": "config.port",
                "canonical_slot": "config.port",
                "fact_hash": f"hash-{claim_id}",
                "conflict_key": "service-port",
                "conflict_key_version": 3,
                "recorded_from": NOW,
                "status": "disputed",
                "source_authority": authority,
                "confidence": 0.9,
                "scope": "permanent",
                "volatility": "stable",
            }
        )
    connection.execute(
        "INSERT INTO conflict_cases(id,pair_key,left_claim_id,right_claim_id,status,decision,created_at) "
        "VALUES ('case-1','pair-1','left','right','manual_required','uncertain',?)",
        (NOW,),
    )
    connection.commit()


def test_maintenance_queues_one_idempotent_l2_job_without_calling_model(tmp_path: Path) -> None:
    connection = Database(tmp_path / "queue.db").open()
    _manual_pair(connection)

    first = auto_resolve_conflicts(connection, NOW, mode="observe", l1_policy=L1Policy(300, 0.1))
    connection.execute(
        "UPDATE conflict_review_state SET dirty_at=?,dirty_reason='test_requeue' WHERE case_id='case-1'",
        (NOW,),
    )
    connection.commit()
    second = auto_resolve_conflicts(connection, NOW, mode="observe", l1_policy=L1Policy(300, 0.1))

    row = connection.execute("SELECT job_type,payload_json FROM jobs").fetchone()
    assert first["l2_queued"] == 1
    assert second["l2_queued"] == 0
    assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    assert row["job_type"] == "resolve_conflict_llm"
    assert json.loads(row["payload_json"])["input_fingerprint"]
    assert "resolve_conflict_llm" in JOB_HANDLERS


class _FixedJudge:
    def __init__(self, connection, *, mutate: bool = False) -> None:
        self.connection = connection
        self.mutate = mutate

    def judge(self, docket):
        assert self.connection.in_transaction is False
        if self.mutate:
            self.connection.execute("UPDATE claims SET confidence=0.8 WHERE id='left'")
            self.connection.commit()
        return AutoDecision("keep_left", "left", 0.95, "L2", "qwen_consistent", ("evidence-left",), "qwen")


def _queued_payload(connection) -> dict[str, object]:
    auto_resolve_conflicts(connection, NOW, mode="observe", l1_policy=L1Policy(300, 0.1))
    return json.loads(connection.execute("SELECT payload_json FROM jobs").fetchone()[0])


def test_llm_job_runs_outside_transaction_and_observe_does_not_change_claims(tmp_path: Path) -> None:
    connection = Database(tmp_path / "observe.db").open()
    _manual_pair(connection)
    payload = _queued_payload(connection)

    result = run_conflict_llm_job(connection, payload, _FixedJudge(connection), mode="observe", now=NOW)

    assert result["status"] == "observed"
    assert [row[0] for row in connection.execute("SELECT status FROM claims ORDER BY id")] == [
        "disputed",
        "disputed",
    ]
    assert connection.execute("SELECT status FROM governance_actions").fetchone()[0] == "observed"


def test_llm_job_stale_revision_performs_zero_writes(tmp_path: Path) -> None:
    connection = Database(tmp_path / "stale.db").open()
    _manual_pair(connection)
    payload = _queued_payload(connection)

    with pytest.raises(StaleConflictDecision, match="stale"):
        run_conflict_llm_job(connection, payload, _FixedJudge(connection, mutate=True), mode="enforce", now=NOW)

    assert connection.execute("SELECT count(*) FROM governance_actions").fetchone()[0] == 0
    assert connection.execute("SELECT status FROM conflict_cases WHERE id='case-1'").fetchone()[0] == "manual_required"


def test_enforce_rolls_back_claim_mutation_when_ledger_insert_fails(tmp_path: Path) -> None:
    connection = Database(tmp_path / "atomic.db").open()
    _manual_pair(connection)
    payload = _queued_payload(connection)
    connection.execute(
        "CREATE TRIGGER reject_governance BEFORE INSERT ON governance_actions "
        "BEGIN SELECT RAISE(ABORT,'ledger rejected'); END"
    )
    connection.commit()

    with pytest.raises(Exception, match="ledger rejected"):
        run_conflict_llm_job(connection, payload, _FixedJudge(connection), mode="enforce", now=NOW)

    assert [row[0] for row in connection.execute("SELECT status FROM claims ORDER BY id")] == [
        "disputed",
        "disputed",
    ]
    assert connection.execute("SELECT status FROM conflict_cases WHERE id='case-1'").fetchone()[0] == "manual_required"


class _OversizedRunner:
    config = type("Config", (), {"model": "qwen-test"})()

    def run_case(self, docket):
        raise OversizedDocket("too large")


def test_oversized_docket_is_l3_but_transport_failures_are_not_swallowed() -> None:
    decision = LocalConflictJudge(_OversizedRunner()).judge(
        {
            "case": {"id": "case-1", "overflow": 0},
            "claims": [],
            "candidates": [],
            "evidence": [],
            "context": {"docket_oversized": False},
        }
    )

    assert (decision.decision, decision.tier, decision.rule) == ("manual_required", "L3", "oversized_docket")


def test_settings_default_to_observe_and_reject_non_loopback_judge() -> None:
    settings = Settings.for_test()
    assert settings.conflict_auto_mode == "observe"
    with pytest.raises(ConfigurationError, match="loopback"):
        LocalConflictJudge.from_settings(
            replace(Settings.for_test(), maintenance_judge_base_url="https://example.com/v1")
        )
