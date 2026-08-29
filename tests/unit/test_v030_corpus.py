from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from hl_mem.evaluation.v030_corpus import (
    build_manifest,
    load_manifest,
    redact_text,
    validate_manifest,
    write_manifest,
)
from scripts import run_v030_experiments as experiment_runner

run_baseline = experiment_runner.run_baseline
run_e1_experiment = experiment_runner.run_e1_experiment
run_experiments = experiment_runner.main


def _case(case_id: str, *, source: str, category: str, decision: str = "keep_left", **extra: object) -> dict:
    return {
        "case_id": case_id,
        "source": source,
        "category": category,
        "input": {"summary": case_id},
        "gold": {"decision": decision},
        **extra,
    }


def _e1_cases() -> list[dict]:
    local_labels = ["keep_left"] * 28 + ["keep_right"] * 14 + ["coexist"] * 11
    local_labels += ["select_candidate"] * 5 + ["reject"]
    cases = [
        _case(f"local-{index:03}", source="local", category="conflict", decision=decision)
        for index, decision in enumerate(local_labels)
    ]
    cases.extend(
        _case(f"volcano-{index:03}", source="volcano", category="conflict", decision="coexist") for index in range(11)
    )
    return cases


def _minimum_cases(experiment: str) -> list[dict]:
    if experiment == "E1":
        return _e1_cases()
    if experiment == "E2":
        labels = ["equivalent"] * 203 + ["distinct"] * 203
        return [
            _case(f"e2-{index:03}", source="local", category=decision, decision=decision)
            for index, decision in enumerate(labels)
        ]
    if experiment == "E3":
        counts = {
            "correction": 50,
            "guardrail": 50,
            "high_cost": 40,
            "persistent_instruction": 40,
            "bait_negative": 30,
            "ordinary": 30,
        }
    elif experiment == "E4":
        counts = {"unique_alias": 80, "ambiguous": 60, "no_entity": 50, "multi_entity": 50}
    elif experiment == "E5":
        counts = {"complete": 35, "cancel": 25, "replace": 25, "partial": 30, "ambiguous_negative": 25}
    elif experiment == "E6":
        return [
            _case(
                f"e6-{index:03}",
                source="synthetic",
                category="price",
                decision="same_series",
                instrument_id=f"instrument:{index % 20:02}",
            )
            for index in range(120)
        ]
    else:
        raise AssertionError(experiment)
    cases = [
        _case(f"{experiment.lower()}-{category}-{index:03}", source="synthetic", category=category)
        for category, count in counts.items()
        for index in range(count)
    ]
    if experiment == "E5":
        for case, tag in zip(
            cases[:5],
            ("innovation_variants", "gold_10500", "negation", "cross_account", "unordered_partial"),
        ):
            case["risk_tags"] = [tag]
    return cases


def _manifest(experiment: str, cases: list[dict] | None = None) -> dict:
    return build_manifest(
        experiment,
        cases or _minimum_cases(experiment),
        source_snapshots=[{"source_id": "fixture", "sha256": "a" * 64, "reconstructable": True}],
        source_audit={"selection": "test fixture"},
    )


def test_build_manifest_orders_cases_and_produces_repeatable_hash() -> None:
    cases = list(reversed(_e1_cases()))

    first = _manifest("E1", cases)
    second = _manifest("E1", list(reversed(cases)))

    assert first == second
    assert [case["case_id"] for case in first["cases"]] == sorted(case["case_id"] for case in cases)
    assert len(first["manifest_sha256"]) == 64


def test_e1_requires_exactly_seventy_unique_cases_and_source_split() -> None:
    summary = validate_manifest(_manifest("E1"))

    assert summary["case_count"] == 70
    assert summary["source_counts"] == {"local": 59, "volcano": 11}
    assert summary["decision_counts"]["reject"] == 1

    duplicate = _e1_cases()
    duplicate[-1]["case_id"] = duplicate[0]["case_id"]
    with pytest.raises(ValueError, match="unique case_id"):
        build_manifest("E1", duplicate, source_snapshots=[], source_audit={})


@pytest.mark.parametrize("experiment", ["E2", "E3", "E4", "E5", "E6"])
def test_minimum_experiment_contracts_are_accepted(experiment: str) -> None:
    summary = validate_manifest(_manifest(experiment))

    assert summary["case_count"] == len(_minimum_cases(experiment))


def test_minimum_experiment_contracts_fail_closed_when_one_case_is_missing() -> None:
    for experiment in ("E2", "E3", "E4", "E5", "E6"):
        with pytest.raises(ValueError, match=experiment):
            build_manifest(
                experiment,
                _minimum_cases(experiment)[:-1],
                source_snapshots=[],
                source_audit={},
            )


def test_e5_requires_named_risk_scenarios() -> None:
    cases = _minimum_cases("E5")
    for case, tag in zip(
        cases[:5], ("innovation_variants", "gold_10500", "negation", "cross_account", "unordered_partial")
    ):
        case["risk_tags"] = [tag]

    assert validate_manifest(_manifest("E5", cases))["risk_tags"] == [
        "cross_account",
        "gold_10500",
        "innovation_variants",
        "negation",
        "unordered_partial",
    ]


def test_redaction_removes_common_direct_identifiers() -> None:
    raw = (
        "mail user@example.com url https://private.example/a ip 10.1.2.3 path C:\\Users\\secret\\data.json id "
        + "a" * 32
    )

    redacted = redact_text(raw)

    assert redacted == redact_text(raw)
    for secret in ("user@example.com", "private.example", "10.1.2.3", "Users\\secret", "a" * 32):
        assert secret not in redacted


def test_write_load_round_trip_and_tamper_detection(tmp_path: Path) -> None:
    path = tmp_path / "e1.json"
    original = _manifest("E1")

    write_manifest(path, original)

    assert load_manifest(path) == original
    tampered = copy.deepcopy(original)
    tampered["cases"][0]["gold"]["decision"] = "keep_right"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="SHA256"):
        load_manifest(path)


def test_experiment_orchestrator_validates_all_six_manifests(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for experiment in sorted(("E1", "E2", "E3", "E4", "E5", "E6")):
        write_manifest(tmp_path / f"{experiment.lower()}.json", _manifest(experiment))

    assert run_experiments(["validate", "--manifest-dir", str(tmp_path)]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["case_counts"] == {"E1": 70, "E2": 406, "E3": 240, "E4": 240, "E5": 140, "E6": 120}
    assert len(output["manifest_set_sha256"]) == 64


def _baseline_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()
    e1_cases = _e1_cases()
    e1_cases[0]["input"] = {
        "case": {"group_key": None, "left_claim_id": "left", "right_claim_id": "right"},
        "claims": [
            {"id": "left", "source_authority": "high"},
            {"id": "right", "source_authority": "low"},
        ],
        "candidates": [],
    }
    for experiment in sorted(("E1", "E2", "E3", "E4", "E5", "E6")):
        cases = e1_cases if experiment == "E1" else _minimum_cases(experiment)
        write_manifest(manifest_dir / f"{experiment.lower()}.json", _manifest(experiment, cases))

    database = tmp_path / "baseline.db"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE dedup_pairs(decision TEXT, reviewed_at TEXT, applied_at TEXT);
            INSERT INTO dedup_pairs VALUES
                ('equivalent', '2026-08-01', NULL),
                ('distinct', '2026-08-01', NULL),
                (NULL, NULL, NULL);
            CREATE TABLE conflict_cases(id TEXT, status TEXT, resolved_at TEXT);
            INSERT INTO conflict_cases VALUES
                ('stable', 'manual_required', NULL),
                ('dirty', 'manual_required', NULL),
                ('pending', 'pending', NULL);
            CREATE TABLE conflict_review_state(case_id TEXT, dirty_at TEXT);
            INSERT INTO conflict_review_state VALUES ('stable', NULL), ('dirty', '2026-08-25');
            """)
    config = tmp_path / "hl_mem.toml"
    config.write_text("[dedup]\naudit_only = true\n", encoding="utf-8")
    recall = tmp_path / "recall.json"
    recall.write_text(
        json.dumps({"dataset_sha256": "b" * 64, "metrics": {"recall_at_5": 0.8, "mrr": 0.7}}),
        encoding="utf-8",
    )
    return manifest_dir, database, config, recall


def test_baseline_a_arm_is_read_only_and_byte_reproducible(tmp_path: Path) -> None:
    manifest_dir, database, config, recall = _baseline_inputs(tmp_path)
    output_dir = tmp_path / "baseline"
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    first = run_baseline(manifest_dir, database, config, recall, output_dir, arm="A")
    first_bytes = (output_dir / "baseline.json").read_bytes()
    first_sums = (output_dir / "SHA256SUMS").read_bytes()
    second = run_baseline(manifest_dir, database, config, recall, output_dir, arm="A")

    assert first == second
    assert first_bytes == (output_dir / "baseline.json").read_bytes()
    assert first_sums == (output_dir / "SHA256SUMS").read_bytes()
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert first["e1_l0"]["decision_counts"] == {"keep_left": 1, "manual_required": 69}
    assert first["dedup"] == {
        "applied": 0,
        "audit_only": True,
        "decision_counts": {"<null>": 1, "distinct": 1, "equivalent": 1},
        "reviewed": 2,
        "total": 3,
    }
    assert first["conflicts"] == {"open": 3, "stable_manual_required": 1}
    assert first["recall"]["metrics"] == {"mrr": 0.7, "recall_at_5": 0.8}
    assert (output_dir / "summary.md").is_file()


def test_baseline_rejects_non_a_arm(tmp_path: Path) -> None:
    manifest_dir, database, config, recall = _baseline_inputs(tmp_path)

    with pytest.raises(ValueError, match="A arm"):
        run_baseline(manifest_dir, database, config, recall, tmp_path / "baseline", arm="B")


def _e1_orchestrator_fixture(tmp_path: Path) -> tuple[Path, dict[str, dict[str, object]]]:
    manifest = _manifest("E1")
    expected: dict[str, dict[str, object]] = {}
    for item in manifest["cases"]:
        case_id = item["case_id"]
        gold = item["gold"]
        if gold["decision"] == "select_candidate":
            gold["winner_candidate_key"] = "left-value"
        expected[case_id] = gold
        item["input"] = {
            "case": {
                "id": case_id,
                "left_claim_id": "left",
                "right_claim_id": "right",
                "group_key": "group" if gold["decision"] == "select_candidate" else None,
            },
            "claims": [
                {
                    "id": claim_id,
                    "value": f"{claim_id}-value",
                    "status": "disputed",
                    "namespace_key": "default",
                    "subject_entity_id": "subject",
                    "canonical_slot": "config.port",
                    "qualifiers": {"service": "fixture"},
                }
                for claim_id in ("left", "right")
            ],
            "candidates": [
                {"candidate_key": "left-value", "representative_claim_id": "left", "support_count": 1},
                {"candidate_key": "right-value", "representative_claim_id": "right", "support_count": 1},
            ],
            "evidence_refs": {},
        }
    manifest = build_manifest(
        "E1",
        manifest["cases"],
        source_snapshots=manifest["source_snapshots"],
        source_audit=manifest["source_audit"],
    )
    manifest_path = tmp_path / "e1.json"
    write_manifest(manifest_path, manifest)
    return manifest_path, expected


def test_e1_orchestrator_runs_l0_and_l1_arms_without_l2(tmp_path: Path) -> None:
    manifest_path, _expected = _e1_orchestrator_fixture(tmp_path)

    report = run_e1_experiment(manifest_path, tmp_path / "out")

    assert set(report["arms"]) == {"A", "B"}
    assert all(prediction["tier"] != "L2" for arm in report["arms"].values() for prediction in arm["predictions"])
    assert "model_calls" not in report
    assert len(report["cases"]) == 70
    assert all((tmp_path / "out" / name).is_file() for name in ("e1_report.json", "e1_report.md"))


def test_e1_v2_report_is_versioned_sealed_and_summarizes_invariant_conflicts(tmp_path: Path) -> None:
    manifest_path, _expected = _e1_orchestrator_fixture(tmp_path)
    manifest = load_manifest(manifest_path)
    first_case_id = str(manifest["cases"][0]["case_id"])
    overlay_path = tmp_path / "e1_v2.json"
    overlay_path.write_text(
        json.dumps(
            {
                "schema_version": "v030-e1-replay-overlay-v2",
                "base_manifest_sha256": manifest["manifest_sha256"],
                "cases": [],
                "gold_invariant_conflicts": [first_case_id],
            }
        ),
        encoding="utf-8",
    )

    output_dir = tmp_path / "out"
    report = run_e1_experiment(
        manifest_path,
        output_dir,
        replay_overlay_path=overlay_path,
    )

    assert report["schema_version"] == "v030-e1-report-v2"
    assert report["gold_invariant_conflicts"] == {"count": 1, "case_ids": [first_case_id]}
    assert report["arms"]["B"]["gate"]["passed"] is False
    assert report["arms"]["B"]["tier_distribution"]
    assert "l3_reason_distribution" in report["arms"]["B"]
    assert report["remaining_gaps"]["gate_deficits"]["gold_invariant_conflicts"] == 1
    assert report["remaining_gaps"]["next_action"] == "keep_observe_and_preregister_any_followup"
    assert (output_dir / "e1_report.json").exists() is False
    assert all(
        (output_dir / name).is_file()
        for name in ("e1_report_v2.json", "e1_report_v2.md", "SEALED_FAILED_v2", "SHA256SUMS_v2")
    )
    markdown = (output_dir / "e1_report_v2.md").read_text(encoding="utf-8")
    assert "Gold invariant conflicts (1)" in markdown
    assert first_case_id in markdown
    assert "Remaining gaps" in markdown


@pytest.mark.parametrize(
    ("claim", "expected"),
    [
        (
            {"pre_decision_status": "disputed", "status_at_decision": "active", "status": "expired"},
            "disputed",
        ),
        ({"status_at_decision": "disputed", "status": "superseded"}, "disputed"),
        ({"status": "active"}, "active"),
        ({}, "disputed"),
    ],
)
def test_e1_replay_status_prefers_explicit_pre_decision_snapshot(claim: dict[str, str], expected: str) -> None:
    assert experiment_runner._manifest_claim_status(claim) == expected


@pytest.mark.parametrize(
    ("status", "terminal"),
    [
        ("superseded", True),
        ("expired", True),
        ("rejected", True),
        ("rolled_back", True),
        ("disputed", False),
        ("retracted", False),
        ("archived", False),
    ],
)
def test_e1_replay_uses_the_registered_lifecycle_terminal_set(status: str, terminal: bool) -> None:
    item = {
        "case_id": "case-terminal-contract",
        "input": {
            "case": {
                "id": "case-terminal-contract",
                "left_claim_id": "left",
                "right_claim_id": "right",
                "namespace_key": "default",
            },
            "claims": [
                {"id": "left", "status": status, "namespace_key": "default"},
                {"id": "right", "status": "disputed", "namespace_key": "default"},
            ],
        },
    }

    docket = experiment_runner._e1_docket(item)

    assert docket["candidates"][0]["terminal"] is terminal


def test_e1_v2_overlay_is_authenticated_and_keeps_gold_conflicts_explicit(tmp_path: Path) -> None:
    manifest = _manifest("E1")
    target = manifest["cases"][0]
    target["input"] = {"claims": [{"id": "left", "status": "superseded"}]}
    manifest = build_manifest(
        "E1",
        manifest["cases"],
        source_snapshots=manifest["source_snapshots"],
        source_audit=manifest["source_audit"],
    )
    base_path = tmp_path / "e1.json"
    write_manifest(base_path, manifest)
    overlay_path = tmp_path / "e1_v2.json"
    overlay_path.write_text(
        json.dumps(
            {
                "schema_version": "v030-e1-replay-overlay-v2",
                "base_manifest_sha256": manifest["manifest_sha256"],
                "cases": [
                    {
                        "case_id": target["case_id"],
                        "claim_status_overrides": {"left": {"pre_decision_status": "disputed"}},
                    }
                ],
                "gold_invariant_conflicts": ["local-unreconstructable"],
            }
        ),
        encoding="utf-8",
    )

    replay = experiment_runner.load_e1_replay_manifest(base_path, overlay_path)

    claim = replay["cases"][0]["input"]["claims"][0]
    assert claim["pre_decision_status"] == "disputed"
    assert replay["gold_invariant_conflicts"] == ["local-unreconstructable"]
    assert replay["replay_overlay_sha256"] == hashlib.sha256(overlay_path.read_bytes()).hexdigest()
