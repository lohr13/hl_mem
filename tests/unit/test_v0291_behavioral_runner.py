from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evaluation.v0291_behavioral import runner as behavioral_runner
from evaluation.v0291_behavioral.agent import ModelCallResult
from evaluation.v0291_behavioral.aggregate import (
    aggregate_behavioral_results,
    relative_reduction,
)
from evaluation.v0291_behavioral.manifest import (
    expand_behavioral_samples,
    load_behavioral_manifest,
)
from evaluation.v0291_behavioral.packet import materialize_behavioral_arms
from evaluation.v0291_behavioral.runner import (
    BudgetedTransport,
    BudgetExceeded,
    BudgetLedger,
    DuplicateRecord,
    GateBlocked,
    build_blind_review_queue,
    load_unique_jsonl,
    prepare_agent_assignments,
    require_sentinel_gate,
    run_structural_phase,
)
from evaluation.v0291_behavioral.scorer import BehavioralScorer, load_sentinels
from scripts import run_v0291_behavioral_eval as behavioral_eval_script
from scripts.run_v0291_behavioral_eval import main

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "tests/fixtures/v0291_freshness_behavioral.json"
SENTINEL_PATH = ROOT / "tests/fixtures/v0291_judge_sentinels.json"


@pytest.mark.asyncio
async def test_budget_ledger_reserves_before_call_and_reconciles_actual_usage() -> None:
    ledger = BudgetLedger(hard_budget_cny=0.001)
    reservation = await ledger.reserve(
        system_prompt="judge",
        user_payload={"sample": "x"},
        max_output_tokens=50,
    )
    during = ledger.snapshot()

    assert during["outstanding_reservations"] == 1
    assert during["reserved_cny"] > 0
    await ledger.reconcile(reservation, input_tokens=10, output_tokens=5)
    after = ledger.snapshot()
    assert after["outstanding_reservations"] == 0
    assert after["actual_input_tokens"] == 10
    assert after["actual_output_tokens"] == 5
    assert after["spent_cny"] == pytest.approx(0.00006)


@pytest.mark.asyncio
async def test_budget_ledger_hard_stops_and_charges_unknown_failure_conservatively() -> None:
    ledger = BudgetLedger(hard_budget_cny=0.0005)
    reservation = await ledger.reserve(
        system_prompt="x",
        user_payload={"payload": "y"},
        max_output_tokens=50,
    )
    await ledger.charge_reserved(reservation, "transport_unknown")
    charged = ledger.snapshot()

    assert charged["charged_reservation_failures"] == 1
    assert charged["spent_cny"] == charged["conservative_charged_cny"]
    with pytest.raises(BudgetExceeded):
        await ledger.reserve(
            system_prompt="x",
            user_payload={"payload": "y"},
            max_output_tokens=50,
        )


class _OneResultTransport:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def complete(self, **kwargs: Any) -> ModelCallResult:
        if self.fail:
            raise RuntimeError("unknown provider failure")
        return ModelCallResult(
            output={"ok": True},
            request_id="request-1",
            input_tokens=10,
            output_tokens=5,
        )


@pytest.mark.asyncio
async def test_budgeted_transport_reconciles_success_and_charges_unknown_failure() -> None:
    success_ledger = BudgetLedger(hard_budget_cny=0.01)
    result = await BudgetedTransport(_OneResultTransport(), success_ledger).complete(
        system_prompt="system",
        user_payload={"input": "value"},
        schema_name="schema",
        response_schema={"type": "object"},
        max_output_tokens=50,
    )
    assert result.request_id == "request-1"
    assert success_ledger.snapshot()["spent_cny"] == pytest.approx(0.00006)

    failed_ledger = BudgetLedger(hard_budget_cny=0.01)
    with pytest.raises(RuntimeError, match="provider"):
        await BudgetedTransport(_OneResultTransport(fail=True), failed_ledger).complete(
            system_prompt="system",
            user_payload={"input": "value"},
            schema_name="schema",
            response_schema={"type": "object"},
            max_output_tokens=50,
        )
    assert failed_ledger.snapshot()["charged_reservation_failures"] == 1


def test_exact_blind_input_dedup_reuses_only_byte_identical_invocations() -> None:
    manifest = load_behavioral_manifest(MANIFEST_PATH)
    samples = expand_behavioral_samples(manifest)
    packets = materialize_behavioral_arms(manifest, samples)
    assignments, unique_inputs = prepare_agent_assignments(manifest, samples, packets)

    assert len(assignments) == 320
    assert len(unique_inputs) == 131
    assert all(assignment["agent_input_sha256"] in unique_inputs for assignment in assignments)
    for digest, blind in unique_inputs.items():
        assert all(
            assignment["blind_input"] == blind
            for assignment in assignments
            if assignment["agent_input_sha256"] == digest
        )


def test_failed_sentinel_blocks_behavioral_phase_without_pass_early(tmp_path: Path) -> None:
    artifact = tmp_path / "sentinel.json"
    artifact.write_text(
        json.dumps({"passed": False, "valid_count": 0, "matched_count": 0}),
        encoding="utf-8",
    )

    with pytest.raises(GateBlocked, match="9/9"):
        require_sentinel_gate(artifact)


def test_structural_phase_freezes_all_200_by_4_packets(tmp_path: Path) -> None:
    report = run_structural_phase(
        ROOT / "tests/fixtures/v0291_injection_replay.json",
        tmp_path,
    )

    assert report["point_count"] == 200
    assert sum(len(arm["decisions"]) for arm in report["arms"].values()) == 800
    assert (tmp_path / "structural_replay.json").is_file()
    assert (tmp_path / "expanded_structural.jsonl").is_file()


def test_cli_structural_phase_is_zero_cost_and_behavioral_respects_failed_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "run"
    assert main(["--phase", "structural", "--output-dir", str(output)]) == 0
    assert (output / "run_manifest.json").is_file()
    assert (output / "structural_replay.json").is_file()

    failed = output / "sentinel_smoke.json"
    failed.write_text(
        json.dumps({"passed": False, "valid_count": 0, "matched_count": 0}),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert (
        main(
            [
                "--phase",
                "behavioral",
                "--output-dir",
                str(output),
                "--sentinel-artifact",
                str(failed),
            ]
        )
        == 2
    )


@pytest.mark.asyncio
async def test_all_phase_reuses_existing_passing_sentinel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel_artifact = tmp_path / "sentinel.json"
    sentinel_artifact.write_text(
        json.dumps({"passed": True, "valid_count": 9, "matched_count": 9}),
        encoding="utf-8",
    )
    calls: list[str] = []

    class _UnusedBaseTransport:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def aclose(self) -> None:
            calls.append("closed")

    async def fail_if_sentinel_runs(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("passing sentinel artifact must be reused")

    async def record_behavioral(**_kwargs: object) -> dict[str, object]:
        calls.append("behavioral")
        return {}

    monkeypatch.setattr(behavioral_eval_script, "CompatibleStructuredTransport", _UnusedBaseTransport)
    monkeypatch.setattr(behavioral_eval_script, "load_cwd_api_key", lambda: "test-key")
    monkeypatch.setattr(behavioral_eval_script, "run_sentinel_phase", fail_if_sentinel_runs)
    monkeypatch.setattr(behavioral_eval_script, "run_behavioral_phase", record_behavioral)

    exit_code, budget = await behavioral_eval_script._run_paid(
        phase="all",
        output_dir=tmp_path,
        behavior_manifest_path=MANIFEST_PATH,
        sentinel_fixture_path=SENTINEL_PATH,
        sentinel_artifact_path=sentinel_artifact,
        budget_cny=0.01,
    )

    assert exit_code == 0
    assert calls == ["behavioral", "closed"]
    assert budget["spent_cny"] == 0


def test_blind_review_queue_has_three_stale_stable_boundary_without_labels() -> None:
    manifest = load_behavioral_manifest(MANIFEST_PATH)
    samples = expand_behavioral_samples(manifest)
    packets = materialize_behavioral_arms(manifest, samples)
    assignments, _ = prepare_agent_assignments(manifest, samples, packets)
    traces = {
        assignment["agent_input_sha256"]: {
            "trace": [{"source": "final_answer", "content": "blind answer"}],
            "call_status": "ok",
        }
        for assignment in assignments
    }

    queue = build_blind_review_queue(assignments, samples, traces)

    assert len(queue) == 9
    serialized = json.dumps(queue, ensure_ascii=False, sort_keys=True)
    assert "cohort" not in serialized
    assert "arm_name" not in serialized
    assert "current_truth" not in serialized
    assert "expected_applicability" not in serialized


def test_jsonl_resume_rejects_duplicate_valid_keys(tmp_path: Path) -> None:
    path = tmp_path / "records.jsonl"
    record = {"key": "same", "call_status": "ok"}
    path.write_text(
        json.dumps(record) + "\n" + json.dumps(record) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(DuplicateRecord, match="same"):
        load_unique_jsonl(path, key_field="key", valid_status="ok")


def test_judge_resume_allows_legacy_result_key_collision_across_agent_inputs(tmp_path: Path) -> None:
    path = tmp_path / "judge.jsonl"
    rows = [
        {
            "result_key": "legacy-collision",
            "agent_input_sha256": "agent-a",
            "call_status": "ok",
        },
        {
            "result_key": "legacy-collision",
            "agent_input_sha256": "agent-b",
            "call_status": "ok",
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    records = behavioral_runner._load_unique_judge_records(path)

    assert len(records) == 2
    assert {record["agent_input_sha256"] for record in records.values()} == {"agent-a", "agent-b"}

    path.write_text("\n".join(json.dumps(row) for row in [*rows, rows[0]]) + "\n", encoding="utf-8")
    with pytest.raises(DuplicateRecord, match="duplicate valid judge identity"):
        behavioral_runner._load_unique_judge_records(path)


def test_agent_resume_selects_only_records_for_current_schema_aware_inputs() -> None:
    current = {"input_sha256": "current", "call_status": "ok"}
    stale = {"input_sha256": "stale-schema", "call_status": "ok"}

    selected = behavioral_runner._select_current_agent_records(
        {"current": current, "stale-schema": stale},
        {"current": {"model_input": {}}},
    )

    assert selected == {"current": current}


class _FixedJudgeTransport:
    def __init__(self, output: dict[str, Any]) -> None:
        self.output = output

    async def complete(self, **kwargs: Any) -> ModelCallResult:
        return ModelCallResult(
            output=self.output,
            request_id="judge-current",
            input_tokens=10,
            output_tokens=5,
        )


@pytest.mark.asyncio
async def test_judge_resume_reuses_only_records_with_the_current_scorer_identity() -> None:
    sentinel = load_sentinels(SENTINEL_PATH)[0]
    digest = "agent-input-digest"
    scored = await BehavioralScorer(_FixedJudgeTransport(sentinel["expected_judgment"])).score(
        sentinel,
        sentinel["trace"],
    )
    current = {
        "result_key": "current",
        "agent_input_sha256": digest,
        **scored,
    }
    stale = {
        **current,
        "result_key": "stale",
        "prompt_sha256": "old-prompt",
    }

    selected = behavioral_runner._select_current_judge_records(
        {"stale": stale, "current": current},
        {digest: sentinel},
        {digest: {"trace": sentinel["trace"]}},
    )

    assert selected == {digest: current}


def test_judge_result_key_changes_when_the_effective_schema_changes() -> None:
    scored = {
        "input_sha256": "input",
        "prompt_sha256": "prompt",
        "schema_sha256": "schema-v1",
        "model": "model",
    }

    original = behavioral_runner._judge_result_key(scored, agent_input_sha256="agent-a")
    changed_schema = behavioral_runner._judge_result_key(
        {**scored, "schema_sha256": "schema-v2"},
        agent_input_sha256="agent-a",
    )
    changed_agent = behavioral_runner._judge_result_key(scored, agent_input_sha256="agent-b")

    assert changed_schema != original
    assert changed_agent != original


def _judgment(
    sample_id: str,
    *,
    obsolete: str = "not_applicable",
    verification: str = "not_applicable",
    stable: str = "not_applicable",
) -> dict[str, Any]:
    return {
        "schema_version": "hl-mem-staleness-judge-v1",
        "sample_id": sample_id,
        "obsolete_acceptance": obsolete,
        "verification_action": verification,
        "stable_fact_disposition": stable,
        "final_attribution": "not_applicable",
        "unsupported_new_configuration": "not_applicable",
        "confidence": "high",
        "review_reason": "none",
        "evidence": [],
    }


def test_paired_aggregation_reports_literal_counts_discordance_and_cluster_ci() -> None:
    arms = (
        "echo_off__freshness_off",
        "echo_enforce__freshness_off",
        "echo_off__freshness_render",
        "echo_enforce__freshness_render",
    )
    rows: list[dict[str, Any]] = []
    for sample_id in ("sample-stale-a", "sample-stale-b"):
        for arm in arms:
            treatment = "freshness_render" in arm
            rows.append(
                {
                    "opaque_sample_id": sample_id,
                    "scenario_family_id": sample_id,
                    "cohort": "stale_positive",
                    "arm_name": arm,
                    "applicable_dimensions": [
                        "obsolete_acceptance",
                        "verification_action",
                    ],
                    "judge_output": _judgment(
                        sample_id,
                        obsolete=("safe_no_unverified_reliance" if treatment else "unsafe_unverified_reliance"),
                        verification=("proposed_specific_before_action" if treatment else "none"),
                    ),
                }
            )

    report = aggregate_behavioral_results(rows, bootstrap_iterations=200)

    control = report["arms"]["echo_off__freshness_off"]
    treatment = report["arms"]["echo_off__freshness_render"]
    assert control["unsafe_obsolete_acceptance"] == {
        "numerator": 2,
        "denominator": 2,
        "rate": 1.0,
        "ci95": [pytest.approx(0.34238, abs=1e-5), 1.0],
    }
    assert treatment["unsafe_obsolete_acceptance"]["numerator"] == 0
    paired = report["paired"]["freshness_main_effect"]["unsafe_obsolete_acceptance"]
    assert paired["discordant"] == {
        "control_0_treatment_1": 0,
        "control_1_treatment_0": 2,
    }
    assert paired["rate_difference"] == -1.0
    assert paired["cluster_bootstrap_ci95"] == [-1.0, -1.0]
    assert report["paired"]["freshness_main_effect"]["verification_action_rate"]["rate_difference"] == 1.0


def test_zero_control_rate_has_no_provable_relative_benefit() -> None:
    assert relative_reduction(0.0, 0.0) is None
    assert relative_reduction(0.5, 0.25) == 0.5
