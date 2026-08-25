from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from hl_mem.evaluation.v030_corpus import (
    build_manifest,
    load_manifest,
    redact_text,
    validate_manifest,
    write_manifest,
)
from scripts.run_v030_experiments import main as run_experiments


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
