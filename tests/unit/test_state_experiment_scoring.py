import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from hl_mem.evaluation.state_experiment_scoring import (
    check_threshold_satisfiability,
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
    semantic_value = assertion_id.rsplit(":", maxsplit=1)[-1]
    return {
        "assertion_id": assertion_id,
        "source_claim_index": 0,
        "atomic_index": 0,
        "atomicity": "atomic",
        "claim": {
            "source_event_indices": [0],
            "value": semantic_value,
            "evidence_quote": semantic_value,
        },
        "projection": {"coordinate": coordinate},
    }


def _semantic_prediction(
    assertion_id: str,
    coordinate: dict[str, Any] | None,
    *,
    source_event_indices: list[int],
    value: str,
    evidence_quote: str,
) -> dict[str, Any]:
    prediction = _prediction(assertion_id, coordinate)
    prediction["claim"] = {
        "source_event_indices": source_event_indices,
        "value": value,
        "evidence_quote": evidence_quote,
    }
    return prediction


def _semantic_gold_claim(
    assertion_id: str,
    coordinate: dict[str, Any] | None,
    *,
    source_event_index: int,
    state_value: str,
) -> dict[str, Any]:
    return {
        "assertion_id": assertion_id,
        "coordinate": coordinate,
        "source_event_indices": [source_event_index],
        "state_value": state_value,
    }


def _corpus_record(
    sample_id: str,
    texts: list[str],
    *,
    category: str = "software_version",
    subtype: str = "upgrade",
) -> dict[str, Any]:
    return {
        "bundle_id": sample_id,
        "category": category,
        "subtype": subtype,
        "events": [{"event_index": index, "content": {"text": text}} for index, text in enumerate(texts)],
    }


def _score_semantic_fixture(
    gold: list[dict[str, Any]],
    candidate: list[dict[str, Any]],
    *,
    corpus: list[dict[str, Any]],
    persisted_edges: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    return score_protocol(
        gold,
        baseline_predictions={"claim_count": sum(len(row["claims"]) for row in candidate)},
        candidate_runs=[candidate, candidate, candidate],
        persisted_edges=persisted_edges or set(),
        baseline_observations={"current_injected_assertion_ids": []},
        candidate_observations={"current_injected_assertion_ids": []},
        corpus_records=corpus,
    )


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
                _semantic_gold_claim("state-001:old", COORDINATE, source_event_index=0, state_value="old"),
                _semantic_gold_claim("state-001:new", COORDINATE, source_event_index=0, state_value="new"),
            ],
            "expected_supersede_edges": [["state-001:old", "state-001:new"]],
            "counterexample_zero_supersede": False,
            "current_assertion_ids": ["state-001:new"],
            "historical_assertion_ids": ["state-001:old", "state-001:new"],
        },
        {
            "sample_id": "counter-001",
            "atomic_claims": [
                _semantic_gold_claim(
                    "counter-001:node-a", NODE_A_COORDINATE, source_event_index=0, state_value="node-a"
                ),
                _semantic_gold_claim(
                    "counter-001:node-b", NODE_B_COORDINATE, source_event_index=0, state_value="node-b"
                ),
            ],
            "expected_supersede_edges": [],
            "counterexample_zero_supersede": True,
            "current_assertion_ids": ["counter-001:node-a", "counter-001:node-b"],
            "historical_assertion_ids": [],
        },
        {
            "sample_id": "control-001",
            "atomic_claims": [_semantic_gold_claim("control-001:fact", None, source_event_index=0, state_value="fact")],
            "expected_supersede_edges": [],
            "counterexample_zero_supersede": False,
            "current_assertion_ids": [],
            "historical_assertion_ids": [],
        },
    ]


def _corpus() -> list[dict[str, Any]]:
    return [
        _corpus_record("state-001", ["old new"]),
        _corpus_record("counter-001", ["node-a node-b"]),
        _corpus_record("control-001", ["fact"]),
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
        corpus_records=_corpus(),
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
    assert report["metrics"]["inflation_legacy_vs_arm_a"] == 0.0
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
        corpus_records=_corpus(),
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
            "atomic_claims": [
                _semantic_gold_claim("counter-a:c0:a0", NODE_A_COORDINATE, source_event_index=0, state_value="a0")
            ],
            "expected_supersede_edges": [],
            "counterexample_zero_supersede": True,
            "current_assertion_ids": ["counter-a:c0:a0"],
            "historical_assertion_ids": [],
        },
        {
            "bundle_id": "counter-b",
            "atomic_claims": [
                _semantic_gold_claim("counter-b:c0:a0", NODE_B_COORDINATE, source_event_index=0, state_value="a0")
            ],
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
        corpus_records=[
            _corpus_record("counter-a", ["a0"]),
            _corpus_record("counter-b", ["a0"]),
        ],
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
        corpus_records=_corpus(),
    )

    assert report["metrics"]["three_run_coordinate_consistency"] == 1.0


def test_file_scorer_consumes_sealed_gold_and_returns_aggregates_only(tmp_path: Path) -> None:
    gold_path = tmp_path / "state_sealed_gold.jsonl"
    corpus_path = tmp_path / "state_dev_corpus.jsonl"
    gold_path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in _gold()) + "\n",
        encoding="utf-8",
    )
    corpus_path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in _corpus()) + "\n",
        encoding="utf-8",
    )
    candidate = _candidate_run()

    with pytest.raises(ValueError, match="corpus_path is required"):
        score_protocol_file(
            gold_path,
            baseline_predictions={"claim_count": 5},
            candidate_runs=[candidate, candidate, candidate],
            persisted_edges=set(),
            baseline_observations={"current_injected_assertion_ids": []},
            candidate_observations={"current_injected_assertion_ids": []},
        )

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
        corpus_path=corpus_path,
    )

    assert report["schema_version"] == 1
    assert "metrics" in report
    assert "gold_records" not in report
    assert "records" not in report


def test_file_scorer_requires_corpus_before_reading_gold(tmp_path: Path) -> None:
    candidate = _candidate_run()

    with pytest.raises(ValueError, match="corpus_path is required"):
        score_protocol_file(
            tmp_path / "does-not-exist.jsonl",
            baseline_predictions={"claim_count": 5},
            candidate_runs=[candidate, candidate, candidate],
            persisted_edges=set(),
            baseline_observations={"current_injected_assertion_ids": []},
            candidate_observations={"current_injected_assertion_ids": []},
        )

    with pytest.raises(FileNotFoundError, match="missing-corpus"):
        score_protocol_file(
            tmp_path / "missing-gold.jsonl",
            corpus_path=tmp_path / "missing-corpus.jsonl",
            baseline_predictions={"claim_count": 5},
            candidate_runs=[candidate, candidate, candidate],
            persisted_edges=set(),
            baseline_observations={"current_injected_assertion_ids": []},
            candidate_observations={"current_injected_assertion_ids": []},
        )


@pytest.mark.parametrize(
    ("source_event_indices", "value", "evidence_quote"),
    [
        ([0], "gateway version is v9.9", "gateway version v1.0"),
        ([1], "gateway version is v1.0", "gateway version v1.0"),
        ([0], "gateway version is v1.0", "gateway version v9.9"),
    ],
    ids=("state-value", "source-event", "evidence"),
)
def test_same_assertion_id_loses_atomic_and_coordinate_credit_when_semantics_are_tampered(
    source_event_indices: list[int],
    value: str,
    evidence_quote: str,
) -> None:
    assertion_id = "semantic-001:c0:a0"
    gold = [
        {
            "sample_id": "semantic-001",
            "category": "software_version",
            "atomic_claims": [
                _semantic_gold_claim(
                    assertion_id,
                    COORDINATE,
                    source_event_index=0,
                    state_value="v1.0",
                )
            ],
        }
    ]
    candidate = [
        {
            "sample_id": "semantic-001",
            "claims": [
                _semantic_prediction(
                    assertion_id,
                    COORDINATE,
                    source_event_indices=source_event_indices,
                    value=value,
                    evidence_quote=evidence_quote,
                )
            ],
        }
    ]

    report = _score_semantic_fixture(
        gold,
        candidate,
        corpus=[_corpus_record("semantic-001", ["gateway version v1.0", "unrelated event"])],
    )

    assert report["metrics"]["atomic_claim"] == {
        "true_positive": 0,
        "false_positive": 1,
        "false_negative": 1,
        "precision": 0.0,
        "recall": 0.0,
        "f1": 0.0,
    }
    assert report["metrics"]["state_coordinate"]["true_positive"] == 0
    assert report["metrics"]["state_coordinate"]["false_positive"] == 1
    assert report["metrics"]["state_coordinate"]["false_negative"] == 1


def test_supersede_edge_endpoints_require_semantic_matches_not_position_ids() -> None:
    old_id = "semantic-edge:c0:a0"
    new_id = "semantic-edge:c1:a0"
    gold = [
        {
            "sample_id": "semantic-edge",
            "category": "software_version",
            "atomic_claims": [
                _semantic_gold_claim(old_id, COORDINATE, source_event_index=0, state_value="v1.0"),
                _semantic_gold_claim(new_id, COORDINATE, source_event_index=1, state_value="v1.1"),
            ],
            "expected_supersede_edges": [[old_id, new_id]],
        }
    ]
    candidate = [
        {
            "sample_id": "semantic-edge",
            "claims": [
                _semantic_prediction(
                    old_id,
                    COORDINATE,
                    source_event_indices=[0],
                    value="gateway version is v1.0",
                    evidence_quote="gateway version v1.0",
                ),
                _semantic_prediction(
                    new_id,
                    COORDINATE,
                    source_event_indices=[1],
                    value="gateway version is v9.9",
                    evidence_quote="gateway version v9.9",
                ),
            ],
        }
    ]

    report = _score_semantic_fixture(
        gold,
        candidate,
        corpus=[_corpus_record("semantic-edge", ["gateway version v1.0", "gateway version v1.1"])],
        persisted_edges={(old_id, new_id)},
    )

    assert report["metrics"]["supersede_edge"]["true_positive"] == 0
    assert report["metrics"]["supersede_edge"]["false_positive"] == 1
    assert report["metrics"]["supersede_edge"]["false_negative"] == 1


def test_gold_zero_candidate_is_reported_as_category_and_subtype_false_positive() -> None:
    subtypes = (
        "greeting",
        "no_result_query",
        "hypothetical",
        "example",
        "log_noise",
        "unconfirmed_suggestion",
        "sensitive_information",
        "generic_chatter",
    )
    gold = [
        {
            "sample_id": f"gold-zero-{index:03d}",
            "category": "gold_zero",
            "atomic_claims": [],
            "expected_supersede_edges": [],
        }
        for index, _subtype in enumerate(subtypes, start=1)
    ]
    candidate = [
        {
            "sample_id": f"gold-zero-{index:03d}",
            "claims": [
                _semantic_prediction(
                    f"gold-zero-{index:03d}:spurious",
                    None,
                    source_event_indices=[0],
                    value="invented durable fact",
                    evidence_quote=f"fixture {subtype}",
                )
            ],
        }
        for index, subtype in enumerate(subtypes, start=1)
    ]

    report = _score_semantic_fixture(
        gold,
        candidate,
        corpus=[
            _corpus_record(
                f"gold-zero-{index:03d}",
                [f"fixture {subtype}"],
                category="gold_zero",
                subtype=subtype,
            )
            for index, subtype in enumerate(subtypes, start=1)
        ],
    )

    assert report["metrics"]["atomic_claim"]["false_positive"] == len(subtypes)
    assert report["breakdown"]["by_category"]["gold_zero"]["atomic_claim"]["false_positive"] == len(subtypes)
    assert all(
        report["breakdown"]["by_subtype"][f"gold_zero/{subtype}"]["atomic_claim"]["false_positive"] == 1
        for subtype in subtypes
    )
    assert report["mapping_diagnostics"]["gold_zero_false_positives"] == len(subtypes)


def test_top_level_atomic_compound_layout_scores_like_split_layout() -> None:
    sample_id = "compound-layout"
    coordinates = [COORDINATE, NODE_A_COORDINATE, COORDINATE, NODE_A_COORDINATE]
    values = ["v1.0", "queued", "v1.1", "completed"]
    event_indices = [0, 0, 1, 1]
    gold_ids = [
        f"{sample_id}:c0:a0",
        f"{sample_id}:c0:a1",
        f"{sample_id}:c1:a0",
        f"{sample_id}:c1:a1",
    ]
    gold = [
        {
            "sample_id": sample_id,
            "category": "compound_claim",
            "atomic_claims": [
                _semantic_gold_claim(
                    assertion_id,
                    coordinate,
                    source_event_index=event_index,
                    state_value=state_value,
                )
                for assertion_id, coordinate, event_index, state_value in zip(
                    gold_ids, coordinates, event_indices, values, strict=True
                )
            ],
            "expected_supersede_edges": [[gold_ids[0], gold_ids[2]], [gold_ids[1], gold_ids[3]]],
        }
    ]

    def run(ids: list[str]) -> list[dict[str, Any]]:
        return [
            {
                "sample_id": sample_id,
                "claims": [
                    _semantic_prediction(
                        assertion_id,
                        coordinate,
                        source_event_indices=[event_index],
                        value=f"compound state {state_value}",
                        evidence_quote=f"compound state {state_value}",
                    )
                    for assertion_id, coordinate, event_index, state_value in zip(
                        ids, coordinates, event_indices, values, strict=True
                    )
                ],
            }
        ]

    top_level_ids = [f"{sample_id}:c{index}:a0" for index in range(4)]
    corpus = [
        _corpus_record(
            sample_id,
            ["compound state v1.0; compound state queued", "compound state v1.1; compound state completed"],
            category="compound_claim",
            subtype="version_job",
        )
    ]
    top_level = _score_semantic_fixture(
        gold,
        run(top_level_ids),
        corpus=corpus,
        persisted_edges={(top_level_ids[0], top_level_ids[2]), (top_level_ids[1], top_level_ids[3])},
    )
    split = _score_semantic_fixture(
        gold,
        run(gold_ids),
        corpus=corpus,
        persisted_edges={(gold_ids[0], gold_ids[2]), (gold_ids[1], gold_ids[3])},
    )

    for metric in ("atomic_claim", "state_coordinate", "supersede_edge"):
        assert top_level["metrics"][metric] == split["metrics"][metric]
        assert top_level["metrics"][metric]["precision"] == 1.0
        assert top_level["metrics"][metric]["recall"] == 1.0
    assert top_level["mapping_diagnostics"]["layout_remapped_matches"] == 3
    assert split["mapping_diagnostics"]["layout_remapped_matches"] == 0


def test_mapping_diagnostics_separate_extraction_and_mapping_false_negatives() -> None:
    sample_id = "diagnostic-001"
    ids = [f"{sample_id}:c{index}:a0" for index in range(3)]
    gold = [
        {
            "sample_id": sample_id,
            "category": "software_version",
            "atomic_claims": [
                _semantic_gold_claim(ids[index], COORDINATE, source_event_index=index, state_value=f"v1.{index}")
                for index in range(3)
            ],
            "expected_supersede_edges": [[ids[0], ids[1]], [ids[1], ids[2]]],
        }
    ]
    candidate = [
        {
            "sample_id": sample_id,
            "claims": [
                _semantic_prediction(
                    ids[0],
                    NODE_A_COORDINATE,
                    source_event_indices=[0],
                    value="gateway version v1.0",
                    evidence_quote="gateway version v1.0",
                ),
                _semantic_prediction(
                    ids[1],
                    COORDINATE,
                    source_event_indices=[1],
                    value="gateway version v1.1",
                    evidence_quote="gateway version v1.1",
                ),
            ],
        }
    ]

    report = _score_semantic_fixture(
        gold,
        candidate,
        corpus=[
            _corpus_record(
                sample_id,
                ["gateway version v1.0", "gateway version v1.1", "gateway version v1.2"],
            )
        ],
    )

    diagnostics = report["mapping_diagnostics"]
    assert diagnostics["coordinate_false_negatives_from_extraction"] == 1
    assert diagnostics["coordinate_false_negatives_from_mapping"] == 1
    assert diagnostics["coordinate_false_positives_from_mapping"] == 1
    assert diagnostics["edge_false_negatives_from_extraction"] == 1
    assert diagnostics["edge_false_negatives_from_mapping"] == 1


def test_threshold_satisfiability_uses_unmatched_candidates_not_total_candidate_count() -> None:
    result = check_threshold_satisfiability(
        gold_atomic_count=812,
        gold_coordinate_count=692,
        gold_edge_count=392,
        historical_assertion_count=408,
        baseline_claim_count=415,
    )

    assert result["threshold_count"] == 13
    assert result["pairs_checked"] == 78
    assert result["satisfiable"] is True
    assert result["conflicts"] == []
    assert result["bounds"]["claim_inflation"] == {"unmatched_candidate_count": {"upper": 40}}
    assert result["bounds"]["atomic_claim_recall"] == {"candidate_claim_count": {"lower": 772}}
    assert result["bounds"]["state_coordinate_recall"] == {"candidate_claim_count": {"lower": 658}}


def test_duplicate_semantic_prediction_is_counted_as_false_positive() -> None:
    sample_id = "duplicate-semantic"
    gold_id = f"{sample_id}:c0:a0"
    gold = [
        {
            "sample_id": sample_id,
            "atomic_claims": [_semantic_gold_claim(gold_id, COORDINATE, source_event_index=0, state_value="v1.0")],
        }
    ]
    candidate = [
        {
            "sample_id": sample_id,
            "claims": [
                _semantic_prediction(
                    f"{sample_id}:first",
                    COORDINATE,
                    source_event_indices=[0],
                    value="gateway version v1.0",
                    evidence_quote="gateway version v1.0",
                ),
                _semantic_prediction(
                    f"{sample_id}:duplicate",
                    COORDINATE,
                    source_event_indices=[0],
                    value="gateway version v1.0",
                    evidence_quote="gateway version v1.0",
                ),
            ],
        }
    ]

    report = _score_semantic_fixture(
        gold,
        candidate,
        corpus=[_corpus_record(sample_id, ["gateway version v1.0"])],
    )

    assert report["metrics"]["atomic_claim"]["true_positive"] == 1
    assert report["metrics"]["atomic_claim"]["false_positive"] == 1
    assert report["metrics"]["atomic_claim"]["false_negative"] == 0
    assert report["metrics"]["claim_inflation"] == 1.0
    assert report["metrics"]["inflation_legacy_vs_arm_a"] == 0.0
    assert report["thresholds"]["claim_inflation"] == {"operator": "<=", "target": 0.05}
    assert report["checks"]["claim_inflation"]["actual"] == 1.0
    assert report["checks"]["claim_inflation"]["passed"] is False
    assert report["mapping_diagnostics"]["semantic_rejections"]["duplicate_semantic_match"] == 1


def test_candidate_mapping_cannot_change_independently_mapped_baseline_metrics() -> None:
    sample_id = "baseline-independent"
    gold_id = f"{sample_id}:c0:a1"
    layout_id = f"{sample_id}:c1:a0"
    gold = [
        {
            "sample_id": sample_id,
            "atomic_claims": [_semantic_gold_claim(gold_id, None, source_event_index=0, state_value="release-v1")],
            "current_assertion_ids": [gold_id],
        }
    ]
    baseline_run = [
        {
            "sample_id": sample_id,
            "claims": [
                _semantic_prediction(
                    layout_id,
                    None,
                    source_event_indices=[0],
                    value="release-v1",
                    evidence_quote="release-v1",
                )
            ],
        }
    ]
    valid_candidate = baseline_run
    tampered_candidate = [
        {
            "sample_id": sample_id,
            "claims": [
                _semantic_prediction(
                    layout_id,
                    None,
                    source_event_indices=[0],
                    value="release-v9",
                    evidence_quote="release-v1",
                )
            ],
        }
    ]
    corpus = [_corpus_record(sample_id, ["release-v1"])]

    def score(candidate: list[dict[str, Any]]) -> dict[str, Any]:
        return score_protocol(
            gold,
            baseline_predictions={"claim_count": 1, "non_state_assertion_ids": [layout_id]},
            baseline_run=baseline_run,
            candidate_runs=[candidate, candidate, candidate],
            persisted_edges=set(),
            baseline_observations={"current_injected_assertion_ids": [layout_id]},
            candidate_observations={"current_injected_assertion_ids": []},
            corpus_records=corpus,
        )

    valid_report = score(valid_candidate)
    tampered_report = score(tampered_candidate)

    assert valid_report["metrics"]["current_state_stale_injection"]["baseline_rate"] == 0.0
    assert tampered_report["metrics"]["current_state_stale_injection"]["baseline_rate"] == 0.0
    assert valid_report["metrics"]["non_state_extraction"]["baseline_f1"] == 1.0
    assert tampered_report["metrics"]["non_state_extraction"]["baseline_f1"] == 1.0


def test_baseline_run_claim_count_mismatch_fails_closed() -> None:
    sample_id = "baseline-count"
    assertion_id = f"{sample_id}:c0:a0"
    gold = [
        {
            "sample_id": sample_id,
            "atomic_claims": [_semantic_gold_claim(assertion_id, None, source_event_index=0, state_value="fact")],
        }
    ]
    baseline_run = [
        {
            "sample_id": sample_id,
            "claims": [
                _semantic_prediction(
                    assertion_id,
                    None,
                    source_event_indices=[0],
                    value="fact",
                    evidence_quote="fact",
                )
            ],
        }
    ]

    with pytest.raises(ValueError, match="baseline claim_count does not match baseline_run"):
        score_protocol(
            gold,
            baseline_predictions={"claim_count": 999},
            baseline_run=baseline_run,
            candidate_runs=[baseline_run, baseline_run, baseline_run],
            persisted_edges=set(),
            baseline_observations={"current_injected_assertion_ids": []},
            candidate_observations={"current_injected_assertion_ids": []},
            corpus_records=[_corpus_record(sample_id, ["fact"])],
        )


def test_semantic_protocol_scoring_rejects_missing_corpus() -> None:
    candidate = _candidate_run()

    with pytest.raises(ValueError, match="corpus_records are required"):
        score_protocol(
            _gold(),
            baseline_predictions={"claim_count": 5},
            candidate_runs=[candidate, candidate, candidate],
            persisted_edges=set(),
            baseline_observations={"current_injected_assertion_ids": []},
            candidate_observations={"current_injected_assertion_ids": []},
        )


@pytest.mark.parametrize(
    ("sample_id", "event_text", "value", "evidence_quote", "expected_tp", "rejection"),
    [
        (
            "opaque-gold-value",
            "gateway version v1.0",
            "Mars is red",
            "gateway version v1.0",
            0,
            "value_evidence_mismatch",
        ),
        (
            "opaque-positive",
            "gateway 偏好简洁回复",
            "gateway prefers concise replies",
            "gateway 偏好简洁回复",
            1,
            None,
        ),
        (
            "opaque-han-boundary",
            "甲乙",
            "甲-X-乙",
            "甲乙",
            0,
            "value_evidence_mismatch",
        ),
        (
            "opaque-ascii-boundary",
            "cat",
            "catalog",
            "cat",
            0,
            "value_evidence_mismatch",
        ),
    ],
    ids=("unrelated", "grounded-cross-language", "han-boundary", "ascii-token-boundary"),
)
def test_nonliteral_content_anchor_semantics(
    sample_id: str,
    event_text: str,
    value: str,
    evidence_quote: str,
    expected_tp: int,
    rejection: str | None,
) -> None:
    assertion_id = f"{sample_id}:gold"
    gold = [
        {
            "sample_id": sample_id,
            "atomic_claims": [
                _semantic_gold_claim(
                    assertion_id,
                    None,
                    source_event_index=0,
                    state_value="opaque-control-label",
                )
            ],
        }
    ]
    candidate = [
        {
            "sample_id": sample_id,
            "claims": [
                _semantic_prediction(
                    f"{sample_id}:candidate",
                    None,
                    source_event_indices=[0],
                    value=value,
                    evidence_quote=evidence_quote,
                )
            ],
        }
    ]

    report = _score_semantic_fixture(
        gold,
        candidate,
        corpus=[_corpus_record(sample_id, [event_text])],
    )

    atomic = report["metrics"]["atomic_claim"]
    assert atomic["true_positive"] == expected_tp
    assert atomic["false_positive"] == 1 - expected_tp
    assert atomic["false_negative"] == 1 - expected_tp
    if rejection is not None:
        assert report["mapping_diagnostics"]["semantic_rejections"][rejection] == 1


def test_ambiguous_semantic_mapping_is_invariant_to_gold_claim_order() -> None:
    sample_id = "ambiguous-order"
    gold_claims = [
        _semantic_gold_claim(f"{sample_id}:a", COORDINATE, source_event_index=0, state_value="v1.0"),
        _semantic_gold_claim(f"{sample_id}:b", NODE_A_COORDINATE, source_event_index=0, state_value="v1.0"),
    ]
    candidate = [
        {
            "sample_id": sample_id,
            "claims": [
                _semantic_prediction(
                    f"{sample_id}:candidate",
                    COORDINATE,
                    source_event_indices=[0],
                    value="gateway version v1.0",
                    evidence_quote="gateway version v1.0",
                )
            ],
        }
    ]
    corpus = [_corpus_record(sample_id, ["gateway version v1.0"])]

    forward = _score_semantic_fixture(
        [{"sample_id": sample_id, "atomic_claims": gold_claims}],
        candidate,
        corpus=corpus,
    )
    reversed_order = _score_semantic_fixture(
        [{"sample_id": sample_id, "atomic_claims": list(reversed(gold_claims))}],
        candidate,
        corpus=corpus,
    )

    assert forward["metrics"]["atomic_claim"] == reversed_order["metrics"]["atomic_claim"]
    assert forward["metrics"]["state_coordinate"] == reversed_order["metrics"]["state_coordinate"]


def test_missing_corpus_coverage_reports_only_count_not_sample_ids() -> None:
    sample_id = "protected-bundle-007"
    gold = [
        {
            "sample_id": sample_id,
            "atomic_claims": [_semantic_gold_claim(f"{sample_id}:gold", None, source_event_index=0, state_value="v1")],
        }
    ]
    candidate = [{"sample_id": sample_id, "claims": []}]

    with pytest.raises(ValueError, match=r"missing 1 required sample\(s\)") as error:
        score_protocol(
            gold,
            baseline_predictions={"claim_count": 0},
            candidate_runs=[candidate, candidate, candidate],
            persisted_edges=set(),
            baseline_observations={"current_injected_assertion_ids": []},
            candidate_observations={"current_injected_assertion_ids": []},
            corpus_records=[_corpus_record("public-decoy", ["irrelevant"])],
        )

    assert sample_id not in str(error.value)


def test_malformed_baseline_run_uses_projection_validation_before_corpus_coverage() -> None:
    candidate = _candidate_run()

    with pytest.raises(ValueError, match="candidate run samples must be objects"):
        score_protocol(
            _gold(),
            baseline_predictions={"claim_count": 5},
            baseline_run=[None],  # type: ignore[list-item]
            candidate_runs=[candidate, candidate, candidate],
            persisted_edges=set(),
            baseline_observations={"current_injected_assertion_ids": []},
            candidate_observations={"current_injected_assertion_ids": []},
            corpus_records=_corpus(),
        )
