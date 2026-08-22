"""Structured scorer facade for the frozen state-coordinate A/B protocol."""

from __future__ import annotations

import json
from collections.abc import Collection, Hashable, Mapping, Sequence, Set
from pathlib import Path
from typing import Any

from hl_mem.evaluation.state_experiment_projection import _run_projection
from hl_mem.evaluation.state_experiment_stages import (
    _derive_metrics,
    _evaluate_gates,
    _match_ledger,
    _project_inputs,
)
from hl_mem.evaluation.state_experiment_thresholds import (
    THRESHOLDS,
    check_threshold_satisfiability,
)
from hl_mem.evaluation.state_protocol import CountMetrics
from hl_mem.evaluation.state_sqlite_snapshot import readonly_snapshot, table_columns

__all__ = [
    "THRESHOLDS",
    "_run_projection",
    "check_threshold_satisfiability",
    "classification_metrics",
    "load_persisted_edges",
    "score_protocol",
    "score_protocol_file",
]


def classification_metrics(
    gold: Set[Hashable] | Collection[Hashable],
    predicted: Set[Hashable] | Collection[Hashable],
) -> dict[str, int | float]:
    """Return exact-set precision/recall/F1 with a perfect empty/empty score."""

    return CountMetrics.classify(gold, predicted).as_dict()


def load_persisted_edges(
    database_path: str | Path,
    claim_manifest: Mapping[str, str],
) -> dict[str, Any]:
    """Load real supersede edges from one read-only database snapshot."""

    with readonly_snapshot(database_path) as connection:
        claim_columns = table_columns(connection, "claims")
        missing = {"id", "superseded_by_id"} - claim_columns
        if missing:
            raise ValueError(f"claims table is missing structured edge columns: {', '.join(sorted(missing))}")
        raw_claim_edges = {
            (str(row["id"]), str(row["superseded_by_id"]))
            for row in connection.execute("SELECT id,superseded_by_id FROM claims WHERE superseded_by_id IS NOT NULL")
        }
        evidence_columns = table_columns(connection, "evidence_links")
        if {"derived_id", "evidence_id", "relation", "derived_type", "evidence_type"} <= evidence_columns:
            raw_evidence_edges = {
                (str(row["evidence_id"]), str(row["derived_id"]))
                for row in connection.execute(
                    "SELECT derived_id,evidence_id FROM evidence_links "
                    "WHERE relation='supersedes' AND derived_type='claim' AND evidence_type='claim'"
                )
            }
        else:
            raw_evidence_edges = set()
        raw_edges = raw_claim_edges | raw_evidence_edges
        edges = {
            (claim_manifest[old_id], claim_manifest[new_id])
            for old_id, new_id in raw_edges
            if old_id in claim_manifest and new_id in claim_manifest
        }
        unknown_endpoint_edges = sum(
            old_id not in claim_manifest or new_id not in claim_manifest for old_id, new_id in raw_edges
        )
        return {
            "edges": edges,
            "sources": {
                "claims.superseded_by_id": len(raw_claim_edges),
                "evidence_links": len(raw_evidence_edges),
            },
            "unknown_endpoint_edges": unknown_endpoint_edges,
        }


def score_protocol(
    gold_records: Sequence[Mapping[str, Any]],
    *,
    baseline_predictions: Mapping[str, Any],
    candidate_runs: Sequence[Sequence[Mapping[str, Any]]],
    persisted_edges: Collection[tuple[str, str]],
    baseline_observations: Mapping[str, Any],
    candidate_observations: Mapping[str, Any],
    baseline_run: Sequence[Mapping[str, Any]] | None = None,
    corpus_records: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Score one candidate via projection, ledger, metrics, and gate stages."""

    projected = _project_inputs(
        gold_records,
        baseline_predictions=baseline_predictions,
        candidate_runs=candidate_runs,
        baseline_run=baseline_run,
        corpus_records=corpus_records,
    )
    ledger = _match_ledger(projected, persisted_edges)
    derived = _derive_metrics(
        projected,
        ledger,
        baseline_predictions=baseline_predictions,
        baseline_observations=baseline_observations,
        candidate_observations=candidate_observations,
    )
    gates = _evaluate_gates(projected, derived)
    return {
        "schema_version": 1,
        "metrics": derived["metrics"],
        "breakdown": derived["breakdown"],
        "mapping_diagnostics": ledger["mapping_diagnostics"],
        **gates,
    }


def _load_jsonl_objects(path: Path, label: str) -> list[Mapping[str, Any]]:
    records: list[Mapping[str, Any]] = []
    for line_index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, Mapping):
            raise ValueError(f"{label} JSONL line {line_index} must be an object")
        records.append(value)
    return records


def score_protocol_file(
    gold_path: str | Path,
    *,
    baseline_predictions: Mapping[str, Any],
    candidate_runs: Sequence[Sequence[Mapping[str, Any]]],
    persisted_edges: Collection[tuple[str, str]],
    baseline_observations: Mapping[str, Any],
    candidate_observations: Mapping[str, Any],
    baseline_run: Sequence[Mapping[str, Any]] | None = None,
    corpus_path: str | Path | None = None,
) -> dict[str, Any]:
    """Consume dev or sealed gold internally and return aggregate metrics only."""

    if corpus_path is None:
        raise ValueError("corpus_path is required for semantic protocol scoring")
    corpus_records = _load_jsonl_objects(Path(corpus_path), "corpus")
    gold_records = _load_jsonl_objects(Path(gold_path), "gold")
    return score_protocol(
        gold_records,
        baseline_predictions=baseline_predictions,
        candidate_runs=candidate_runs,
        persisted_edges=persisted_edges,
        baseline_observations=baseline_observations,
        candidate_observations=candidate_observations,
        baseline_run=baseline_run,
        corpus_records=corpus_records,
    )
