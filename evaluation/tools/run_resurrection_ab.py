"""Run the deterministic v0.27 archived-memory resurrection A/B."""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from hl_mem.application.recall import RecallService
from hl_mem.ingest.embedder import pack_vector
from hl_mem.observability.audit import audit_scope
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database

NOW = "2026-08-15T00:00:00+00:00"


class _Embedder:
    dim = 2
    model = "resurrection-ab-deterministic"

    def embed_one(self, _text: str) -> bytes:
        return pack_vector([1.0, 0.0])

    def embed_batch(self, texts: list[str]) -> list[bytes]:
        return [self.embed_one(text) for text in texts]


class _AuditCapture:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, phase: str, action: str, outcome: str, **dimensions: Any) -> bool:
        self.events.append({"phase": phase, "action": action, "outcome": outcome, **dimensions})
        return True


@dataclass(frozen=True)
class FixtureCase:
    case_id: str
    expected: str
    status: str = "archived"
    scope: str = "permanent"
    valid_to: str | None = None
    source: bool = True
    active_rival: bool = False


CASES = (
    FixtureCase("healthy_permanent", "resurrect"),
    FixtureCase("healthy_temporal_valid", "resurrect", scope="temporal"),
    FixtureCase("retracted_terminal", "reject", status="retracted"),
    FixtureCase("superseded_terminal", "reject", status="superseded"),
    FixtureCase("expired_terminal", "reject", status="expired"),
    FixtureCase("expired_valid_time", "reject", valid_to="2026-08-01T00:00:00+00:00"),
    FixtureCase("missing_source", "reject", source=False),
    FixtureCase("active_conflict_rival", "reject", active_rival=True),
)


def _settings(mode: str) -> Settings:
    return replace(
        Settings.for_test(),
        resurrection_mode=mode,
        resurrection_candidate_limit=3,
        resurrection_min_term_coverage=1.0,
        recall_dense_enabled=False,
        reranker_mode="off",
        query_expansion_mode="off",
        relation_expansion_mode="off",
    )


def _build_seed(path: Path, case: FixtureCase) -> None:
    database = Database(path)
    connection = database.open()
    repository = ClaimRepository(connection)
    connection.execute(
        "INSERT INTO events(id,event_type,actor_type,content_json,occurred_at,recorded_at) " "VALUES (?,?,?,?,?,?)",
        (
            f"event-{case.case_id}",
            "message",
            "user",
            '{"text":"atlas cobalt"}',
            NOW,
            NOW,
        ),
    )
    conflict_key = "default:config.model:atlas" if case.active_rival else None
    repository.insert_claim(
        {
            "id": f"claim-{case.case_id}",
            "namespace_key": "default",
            "subject_entity_id": "atlas",
            "predicate": "uses",
            "value": "cobalt",
            "recorded_from": "2026-01-01T00:00:00+00:00",
            "valid_from": "2026-01-01T00:00:00+00:00",
            "valid_to": case.valid_to,
            "status": case.status,
            "confidence": 0.73,
            "scope": case.scope,
            "conflict_key": conflict_key,
            "canonical_slot": "config.model" if conflict_key else None,
            "embedding_dense": None,
        },
        commit=False,
    )
    if case.source:
        connection.execute(
            "INSERT INTO evidence_links(id,derived_type,derived_id,evidence_type,evidence_id,relation) "
            "VALUES (?,?,?,?,?,?)",
            (
                f"link-{case.case_id}",
                "claim",
                f"claim-{case.case_id}",
                "event",
                f"event-{case.case_id}",
                "derived_from",
            ),
        )
    if case.active_rival:
        repository.insert_claim(
            {
                "id": f"rival-{case.case_id}",
                "namespace_key": "default",
                "subject_entity_id": "unrelated",
                "predicate": "uses",
                "value": "saffron",
                "recorded_from": NOW,
                "status": "active",
                "confidence": 0.9,
                "scope": "permanent",
                "conflict_key": conflict_key,
                "canonical_slot": "config.model",
            },
            commit=False,
        )
    connection.commit()
    database.close()


def _run_once(database_path: Path, mode: str, case: FixtureCase) -> dict[str, Any]:
    database = Database(database_path)
    connection = database.open()
    audit = _AuditCapture()
    started = time.perf_counter_ns()
    with audit_scope(audit, query_id=f"{mode}-{case.case_id}"):
        response = RecallService(
            connection,
            _Embedder(),
            settings=_settings(mode),
        ).recall("atlas cobalt", as_of=NOW, debug=True)
    latency_ms = (time.perf_counter_ns() - started) / 1_000_000
    target = f"claim-{case.case_id}"
    returned_ids = [str(result["id"]) for result in response["results"]]
    row = connection.execute("SELECT status,confidence FROM claims WHERE id=?", (target,)).fetchone()
    resurrection_events = [
        event
        for event in audit.events
        if event["phase"] == "recall" and event["action"] == "resurrection" and event["outcome"] == "resurrected"
    ]
    correct_resurrection = case.expected == "resurrect" and target in returned_ids and row[0] == "active"
    incorrect_resurrection = case.expected == "reject" and (
        target in returned_ids or row[0] == "active" or bool(resurrection_events)
    )
    audit_ok = (
        len(resurrection_events) == 1
        and resurrection_events[0].get("claim_id") == target
        and resurrection_events[0].get("detail", {}).get("confidence_changed") is False
        if correct_resurrection
        else not resurrection_events
    )
    trace = response.get("search_trace") or {}
    database.close()
    return {
        "arm": mode,
        "case_id": case.case_id,
        "expected": case.expected,
        "latency_ms": latency_ms,
        "returned_ids": returned_ids,
        "final_status": row[0],
        "final_confidence": row[1],
        "correct_resurrection": correct_resurrection,
        "incorrect_resurrection": incorrect_resurrection,
        "cold_path_attempted": mode == "auto",
        "cold_fts_count": (trace.get("candidate_counts") or {}).get("cold_fts", 0),
        "audit_ok": audit_ok,
        "audit_events": audit.events,
    }


def _percentile_95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int(0.95 * len(ordered) + 0.999999) - 1))]


def run(output_dir: Path, repetitions: int) -> dict[str, Any]:
    work = output_dir / "resurrection_ab_work"
    work.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for case in CASES:
        seed_path = work / f"seed-{case.case_id}.db"
        if seed_path.exists():
            seed_path.unlink()
        _build_seed(seed_path, case)
        for mode in ("off", "auto"):
            for repetition in range(repetitions):
                run_path = work / f"{mode}-{case.case_id}-{repetition}.db"
                if run_path.exists():
                    run_path.unlink()
                shutil.copy2(seed_path, run_path)
                row = _run_once(run_path, mode, case)
                row["repetition"] = repetition
                rows.append(row)
                run_path.unlink()
    arms: dict[str, Any] = {}
    for mode in ("off", "auto"):
        selected = [row for row in rows if row["arm"] == mode]
        first_runs = [row for row in selected if row["repetition"] == 0]
        arms[mode] = {
            "correct_resurrections": sum(bool(row["correct_resurrection"]) for row in first_runs),
            "incorrect_resurrections": sum(bool(row["incorrect_resurrection"]) for row in first_runs),
            "incorrect_resurrection_rate": sum(bool(row["incorrect_resurrection"]) for row in first_runs)
            / sum(row["expected"] == "reject" for row in first_runs),
            "cold_path_trigger_rate": sum(bool(row["cold_path_attempted"]) for row in first_runs) / len(first_runs),
            "p95_latency_ms": _percentile_95([float(row["latency_ms"]) for row in selected]),
            "median_latency_ms": statistics.median(float(row["latency_ms"]) for row in selected),
            "audit_integrity": all(bool(row["audit_ok"]) for row in first_runs),
        }
    answer_gain = arms["auto"]["correct_resurrections"] - arms["off"]["correct_resurrections"]
    gate = {
        "zero_incorrect_resurrections": arms["auto"]["incorrect_resurrections"] == 0,
        "minimum_one_correct_resurrection": arms["auto"]["correct_resurrections"] >= 1,
        "audit_integrity": arms["auto"]["audit_integrity"],
    }
    report = {
        "schema_version": "resurrection-ab-report-v1",
        "fixture_case_count": len(CASES),
        "repetitions_per_case": repetitions,
        "answer_gain_cases": answer_gain,
        "arms": arms,
        "gate": gate,
        "gate_passed": all(gate.values()),
        "recommendation": "auto" if all(gate.values()) else "off",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "resurrection_ab_runs.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    (output_dir / "resurrection_ab_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("var/eval"))
    parser.add_argument("--repetitions", type=int, default=20)
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error("--repetitions must be positive")
    print(json.dumps(run(args.output_dir, args.repetitions), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
