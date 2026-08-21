import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from hl_mem.evaluation.state_experiment_scoring import (
    classification_metrics,
    load_persisted_edges,
    score_protocol,
    score_protocol_file,
)

COORDINATE = {
    "namespace": "default",
    "canonical_subject": "gateway",
    "canonical_slot": "config.version",
    "coordinate_qualifiers": {},
}
NODE_A_COORDINATE = {
    **COORDINATE,
    "coordinate_qualifiers": {"instance": "node-a"},
}
NODE_B_COORDINATE = {
    **COORDINATE,
    "coordinate_qualifiers": {"instance": "node-b"},
}


def _prediction(assertion_id: str, coordinate: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "assertion_id": assertion_id,
        "source_claim_index": 0,
        "atomic_index": 0,
        "atomicity": "atomic",
        "claim": {},
        "projection": {"coordinate": coordinate},
    }


def _candidate_run() -> list[dict[str, Any]]:
    return [
        {
            "sample_id": "state-001",
            "arm": "B1",
            "input_claim_count": 2,
            "output_claim_count": 2,
            "claims": [
                _prediction("state-001:old", COORDINATE),
                _prediction("state-001:new", COORDINATE),
            ],
            "rejections": [],
        },
        {
            "sample_id": "counter-001",
            "arm": "B1",
            "input_claim_count": 2,
            "output_claim_count": 2,
            "claims": [
                _prediction("counter-001:node-a", NODE_A_COORDINATE),
                _prediction("counter-001:node-b", NODE_B_COORDINATE),
            ],
            "rejections": [],
        },
        {
            "sample_id": "control-001",
            "arm": "B1",
            "input_claim_count": 1,
            "output_claim_count": 1,
            "claims": [_prediction("control-001:fact", None)],
            "rejections": [],
        },
    ]


def _gold() -> list[dict[str, Any]]:
    return [
        {
            "sample_id": "state-001",
            "atomic_claims": [
                {"assertion_id": "state-001:old", "coordinate": COORDINATE},
                {"assertion_id": "state-001:new", "coordinate": COORDINATE},
            ],
            "expected_supersede_edges": [["state-001:old", "state-001:new"]],
            "counterexample_zero_supersede": False,
            "current_assertion_ids": ["state-001:new"],
            "historical_assertion_ids": ["state-001:old", "state-001:new"],
        },
        {
            "sample_id": "counter-001",
            "atomic_claims": [
                {"assertion_id": "counter-001:node-a", "coordinate": NODE_A_COORDINATE},
                {"assertion_id": "counter-001:node-b", "coordinate": NODE_B_COORDINATE},
            ],
            "expected_supersede_edges": [],
            "counterexample_zero_supersede": True,
            "current_assertion_ids": ["counter-001:node-a", "counter-001:node-b"],
            "historical_assertion_ids": [],
        },
        {
            "sample_id": "control-001",
            "atomic_claims": [{"assertion_id": "control-001:fact", "coordinate": None}],
            "expected_supersede_edges": [],
            "counterexample_zero_supersede": False,
            "current_assertion_ids": [],
            "historical_assertion_ids": [],
        },
    ]


def test_classification_metrics_uses_structural_set_identity() -> None:
    metrics = classification_metrics({"a", "b"}, {"b", "c"})

    assert metrics == {
        "true_positive": 1,
        "false_positive": 1,
        "false_negative": 1,
        "precision": 0.5,
        "recall": 0.5,
        "f1": 0.5,
    }
    assert classification_metrics(set(), set())["f1"] == 1.0


def test_load_persisted_edges_reads_real_columns_and_ignores_audit_text(tmp_path: Path) -> None:
    database_path = tmp_path / "experiment.db"
    connection = sqlite3.connect(database_path)
    connection.executescript("""
        CREATE TABLE claims(id TEXT PRIMARY KEY, superseded_by_id TEXT);
        CREATE TABLE evidence_links(
            derived_id TEXT,
            evidence_id TEXT,
            relation TEXT,
            derived_type TEXT,
            evidence_type TEXT
        );
        CREATE TABLE audit_log(message TEXT);
        INSERT INTO claims VALUES ('db-old', 'db-new'), ('db-new', NULL),
            ('db-node-a', NULL), ('db-node-b', NULL);
        INSERT INTO evidence_links VALUES
            ('db-new', 'db-old', 'supersedes', 'claim', 'claim'),
            ('db-node-b', 'db-node-a', 'supports', 'claim', 'claim');
        INSERT INTO audit_log VALUES
            ('snapshot_advance db-node-a superseded_by_id db-node-b');
        """)
    connection.commit()
    connection.close()

    result = load_persisted_edges(
        database_path,
        {
            "db-old": "state-001:old",
            "db-new": "state-001:new",
            "db-node-a": "counter-001:node-a",
            "db-node-b": "counter-001:node-b",
        },
    )

    assert result["edges"] == {("state-001:old", "state-001:new")}
    assert result["sources"] == {"claims.superseded_by_id": 1, "evidence_links": 1}
    assert result["unknown_endpoint_edges"] == 0


def test_protocol_scorer_maps_every_frozen_threshold_to_structured_metrics() -> None:
    candidate = _candidate_run()
    report = score_protocol(
        _gold(),
        baseline_predictions={
            "atomic_assertion_ids": [
                "state-001:old",
                "state-001:new",
                "counter-001:node-a",
                "counter-001:node-b",
                "control-001:fact",
            ],
            "non_state_assertion_ids": ["control-001:fact"],
            "claim_count": 5,
        },
        candidate_runs=[candidate, candidate, candidate],
        persisted_edges={("state-001:old", "state-001:new")},
        baseline_observations={
            "current_injected_assertion_ids": [
                "state-001:old",
                "state-001:new",
                "counter-001:node-a",
                "counter-001:node-b",
            ]
        },
        candidate_observations={
            "current_injected_assertion_ids": [
                "state-001:new",
                "counter-001:node-a",
                "counter-001:node-b",
            ],
            "historical_retrieved_assertion_ids": ["state-001:old", "state-001:new"],
        },
    )

    assert report["metrics"]["state_coordinate"]["precision"] == 1.0
    assert report["metrics"]["state_coordinate"]["recall"] == 1.0
    assert report["metrics"]["atomic_claim"]["precision"] == 1.0
    assert report["metrics"]["atomic_claim"]["recall"] == 1.0
    assert report["metrics"]["supersede_edge"]["precision"] == 1.0
    assert report["metrics"]["supersede_edge"]["recall"] == 1.0
    assert report["metrics"]["counterexample_cross_coordinate_supersede"] == 0
    assert report["metrics"]["current_state_stale_injection"] == {
        "baseline_rate": 0.25,
        "candidate_rate": 0.0,
        "reduction": 1.0,
    }
    assert report["metrics"]["historical_old_snapshot_recall"] == 1.0
    assert report["metrics"]["non_state_extraction"]["f1_drop"] == 0.0
    assert report["metrics"]["claim_inflation"] == 0.0
    assert report["metrics"]["three_run_coordinate_consistency"] == 1.0
    assert set(report["thresholds"]) == {
        "state_coordinate_precision",
        "state_coordinate_recall",
        "atomic_claim_precision",
        "atomic_claim_recall",
        "supersede_edge_precision",
        "supersede_edge_recall",
        "counterexample_cross_coordinate_supersede",
        "stale_injection_reduction",
        "stale_injection_absolute",
        "historical_old_snapshot_recall",
        "non_state_f1_drop",
        "claim_inflation",
        "three_run_coordinate_consistency",
    }
    assert report["passed"] is True
    assert all(check["passed"] for check in report["checks"].values())


def test_protocol_scorer_fails_cross_coordinate_edge_and_coordinate_drift() -> None:
    first = _candidate_run()
    second = _candidate_run()
    third = _candidate_run()
    third[1]["claims"][1]["projection"]["coordinate"] = NODE_A_COORDINATE

    report = score_protocol(
        _gold(),
        baseline_predictions={
            "atomic_assertion_ids": ["control-001:fact"],
            "non_state_assertion_ids": ["control-001:fact"],
            "claim_count": 1,
        },
        candidate_runs=[first, second, third],
        persisted_edges={
            ("state-001:old", "state-001:new"),
            ("counter-001:node-a", "counter-001:node-b"),
        },
        baseline_observations={"current_injected_assertion_ids": ["state-001:old"]},
        candidate_observations={
            "current_injected_assertion_ids": ["state-001:old"],
            "historical_retrieved_assertion_ids": [],
        },
    )

    assert report["metrics"]["counterexample_cross_coordinate_supersede"] == 1
    assert report["metrics"]["three_run_coordinate_consistency"] == pytest.approx(0.75)
    assert report["checks"]["counterexample_cross_coordinate_supersede"]["passed"] is False
    assert report["checks"]["three_run_coordinate_consistency"]["passed"] is False
    assert report["passed"] is False


def test_corpus_bundle_id_scopes_counterexample_edges_when_sample_id_is_absent() -> None:
    gold = [
        {
            "bundle_id": "counter-a",
            "atomic_claims": [{"assertion_id": "counter-a:c0:a0", "coordinate": NODE_A_COORDINATE}],
            "expected_supersede_edges": [],
            "counterexample_zero_supersede": True,
            "current_assertion_ids": ["counter-a:c0:a0"],
            "historical_assertion_ids": [],
        },
        {
            "bundle_id": "counter-b",
            "atomic_claims": [{"assertion_id": "counter-b:c0:a0", "coordinate": NODE_B_COORDINATE}],
            "expected_supersede_edges": [],
            "counterexample_zero_supersede": True,
            "current_assertion_ids": ["counter-b:c0:a0"],
            "historical_assertion_ids": [],
        },
    ]
    candidate = [
        {"sample_id": "counter-a", "claims": [_prediction("counter-a:c0:a0", NODE_A_COORDINATE)]},
        {"sample_id": "counter-b", "claims": [_prediction("counter-b:c0:a0", NODE_B_COORDINATE)]},
    ]

    report = score_protocol(
        gold,
        baseline_predictions={"claim_count": 2, "non_state_assertion_ids": []},
        candidate_runs=[candidate, candidate, candidate],
        persisted_edges={("counter-a:c0:a0", "counter-b:c0:a0")},
        baseline_observations={"current_injected_assertion_ids": []},
        candidate_observations={"current_injected_assertion_ids": []},
    )

    assert report["metrics"]["counterexample_cross_coordinate_supersede"] == 0


def test_coordinate_consistency_is_invariant_to_claim_order() -> None:
    first = _candidate_run()
    reordered = _candidate_run()
    reordered[1]["claims"][0]["projection"]["coordinate"] = NODE_B_COORDINATE
    reordered[1]["claims"][1]["projection"]["coordinate"] = NODE_A_COORDINATE

    report = score_protocol(
        _gold(),
        baseline_predictions={
            "atomic_assertion_ids": ["control-001:fact"],
            "non_state_assertion_ids": ["control-001:fact"],
            "claim_count": 5,
        },
        candidate_runs=[first, reordered, first],
        persisted_edges={("state-001:old", "state-001:new")},
        baseline_observations={"current_injected_assertion_ids": []},
        candidate_observations={
            "current_injected_assertion_ids": [],
            "historical_retrieved_assertion_ids": ["state-001:old", "state-001:new"],
        },
    )

    assert report["metrics"]["three_run_coordinate_consistency"] == 1.0


def test_file_scorer_consumes_sealed_gold_and_returns_aggregates_only(tmp_path: Path) -> None:
    gold_path = tmp_path / "state_sealed_gold.jsonl"
    gold_path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in _gold()) + "\n",
        encoding="utf-8",
    )
    candidate = _candidate_run()

    report = score_protocol_file(
        gold_path,
        baseline_predictions={
            "non_state_assertion_ids": ["control-001:fact"],
            "claim_count": 5,
        },
        candidate_runs=[candidate, candidate, candidate],
        persisted_edges={("state-001:old", "state-001:new")},
        baseline_observations={"current_injected_assertion_ids": ["state-001:old"]},
        candidate_observations={
            "current_injected_assertion_ids": [
                "state-001:new",
                "counter-001:node-a",
                "counter-001:node-b",
            ],
            "historical_retrieved_assertion_ids": ["state-001:old", "state-001:new"],
        },
    )

    assert report["schema_version"] == 1
    assert "metrics" in report
    assert "gold_records" not in report
    assert "records" not in report
