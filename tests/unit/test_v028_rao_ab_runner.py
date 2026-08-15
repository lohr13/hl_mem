from __future__ import annotations

import importlib
from types import SimpleNamespace

runner = importlib.import_module("evaluation.tools.run_v028_rao_extraction_ab")


def _metrics(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "source_bounded_rao_rate": 0.0,
        "exact_rao_rate": 0.1,
        "claim_yield_per_event": 1.0,
        "nonrelation_claim_yield_per_event": 0.8,
        "canonical_slot_mismatch_rate": 0.02,
        "packet_rao_completeness": 0.1,
        "entity_coverage_at_5": 0.3,
        "legacy_anchor_coverage": 0.5,
        "forbidden_violations": 0,
        "modality_violations": 0,
        "provenance_violations": 0,
        "leakage_violations": 0,
    }
    value.update(overrides)
    return value


def test_release_gates_require_strict_relation_and_entity_improvement_without_regressions() -> None:
    old = _metrics()
    new = _metrics(
        source_bounded_rao_rate=0.9,
        exact_rao_rate=0.3,
        claim_yield_per_event=1.02,
        nonrelation_claim_yield_per_event=0.79,
        canonical_slot_mismatch_rate=0.01,
        packet_rao_completeness=0.4,
        entity_coverage_at_5=0.4,
        legacy_anchor_coverage=0.5,
    )

    gates = runner.evaluate_release_gates(
        old,
        new,
        relation_coverage_passed=True,
        packet_smoke_passed=True,
    )

    assert gates["passed"] is True
    assert all(item["passed"] for item in gates["checks"])


def test_release_gates_fail_on_equal_entity_coverage_or_any_safety_violation() -> None:
    old = _metrics()
    new = _metrics(
        source_bounded_rao_rate=0.9,
        exact_rao_rate=0.3,
        packet_rao_completeness=0.4,
        entity_coverage_at_5=0.3,
        forbidden_violations=1,
    )

    gates = runner.evaluate_release_gates(
        old,
        new,
        relation_coverage_passed=True,
        packet_smoke_passed=True,
    )

    assert gates["passed"] is False
    failed = {item["id"] for item in gates["checks"] if not item["passed"]}
    assert {"entity_coverage_net_gain", "safety_zero_violations"}.issubset(failed)


def test_extraction_task_order_is_deterministic_and_interleaves_arms() -> None:
    trajectories = [{"trajectory_id": "t1"}, {"trajectory_id": "t2"}, {"trajectory_id": "t3"}]

    first = runner.extraction_task_order(trajectories, preregistration_id="frozen")
    second = runner.extraction_task_order(trajectories, preregistration_id="frozen")

    assert first == second
    assert {(item["trajectory_id"], item["extraction_arm"]) for item in first} == {
        ("t1", "old"),
        ("t1", "new"),
        ("t2", "old"),
        ("t2", "new"),
        ("t3", "old"),
        ("t3", "new"),
    }
    assert first[:3] != [
        {"trajectory_id": "t1", "extraction_arm": "old"},
        {"trajectory_id": "t2", "extraction_arm": "old"},
        {"trajectory_id": "t3", "extraction_arm": "old"},
    ]


def test_relation_discovery_sources_are_the_gold_free_c0_top5_union(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    packets = {
        "case-1": ({"claim_id": "a"}, {"claim_id": "b"}),
        "case-2": ({"claim_id": "b"}, {"claim_id": "c"}),
    }

    def fake_recall(case, settings, embedder, reranker, *, db_path, arm_id):
        del settings, embedder, reranker, db_path
        calls.append((case["case_id"], arm_id))
        return SimpleNamespace(seed_packet=packets[case["case_id"]])

    monkeypatch.setattr(runner, "recall_visible_case", fake_recall)

    selected = runner.relation_discovery_seed_ids(
        [{"case_id": "case-1"}, {"case_id": "case-2"}],
        settings=object(),
        embedder=object(),
        reranker=object(),
        db_path=object(),
    )

    assert selected == ["a", "b", "c"]
    assert calls == [("case-1", "C0"), ("case-2", "C0")]


def test_relation_discovery_source_selector_rejects_non_top5_packet(monkeypatch) -> None:
    def fake_recall(*args, **kwargs):
        del args, kwargs
        return SimpleNamespace(seed_packet=tuple({"claim_id": str(index)} for index in range(6)))

    monkeypatch.setattr(runner, "recall_visible_case", fake_recall)

    try:
        runner.relation_discovery_seed_ids(
            [{"case_id": "case-1"}],
            settings=object(),
            embedder=object(),
            reranker=None,
            db_path=object(),
        )
    except RuntimeError as error:
        assert "Top-5" in str(error)
    else:
        raise AssertionError("oversized seed packet must fail closed")
