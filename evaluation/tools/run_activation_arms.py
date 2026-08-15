"""Replay the three frozen decay arms against one deterministic claim fixture."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from hl_mem.ingest.embedder import pack_vector
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.workers.decay import decay_claims

START = datetime(2025, 1, 1, tzinfo=timezone.utc)
ARMS = ("legacy_linear", "activation_halflife", "confidence_halflife")


@dataclass(frozen=True)
class ClaimFixture:
    claim_id: str
    scope: str
    label: str
    canonical_attribute: str
    access_every_days: int | None = None
    final_access_day: int | None = None


FIXTURES = (
    ClaimFixture("identity_core", "permanent", "must_keep", "identity.name", 120),
    ClaimFixture("permanent_reference", "permanent", "must_keep", "fact.reference", 60),
    ClaimFixture("temporal_recent_hits", "temporal", "must_keep", "state.focus", 21),
    ClaimFixture("temporal_stale", "temporal", "must_archive", "state.stale"),
    ClaimFixture("temporal_one_shot", "temporal", "must_archive", "state.one_shot", 30, 30),
    ClaimFixture("permanent_obsolete", "permanent", "must_archive", "fact.obsolete"),
)


def _decay_kwargs(settings: Settings, arm: str) -> dict[str, Any]:
    return {
        "temporal_decay_days": settings.decay_temporal_days,
        "temporal_archive_days": settings.archive_temporal_days,
        "permanent_decay_days": settings.decay_permanent_days,
        "permanent_archive_days": settings.archive_permanent_days,
        "access_bonus_every": settings.access_bonus_every,
        "access_bonus_days": settings.access_bonus_days,
        "access_bonus_cap_days": settings.access_bonus_cap_days,
        "rollout_grace_days": settings.decay_rollout_grace_days,
        "min_confidence": settings.decay_min_confidence,
        "feedback_lifecycle_mode": settings.feedback_lifecycle_mode,
        "feedback_bonus_cap_days": settings.feedback_bonus_cap_days,
        "decay_model": arm,
        "temporal_half_life_days": settings.decay_temporal_half_life_days,
        "permanent_half_life_days": settings.decay_permanent_half_life_days,
        "identity_half_life_days": settings.decay_identity_half_life_days,
        "halflife_archive_threshold": settings.decay_halflife_archive_threshold,
        "halflife_archive_grace_days": settings.decay_halflife_archive_grace_days,
    }


def _build_database(path: Path) -> Database:
    database = Database(path)
    connection = database.open()
    repository = ClaimRepository(connection)
    timestamp = START.isoformat()
    for fixture in FIXTURES:
        repository.insert_claim(
            {
                "id": fixture.claim_id,
                "namespace_key": "default",
                "subject_entity_id": fixture.claim_id,
                "predicate": "has_state",
                "value": fixture.claim_id,
                "recorded_from": timestamp,
                "last_accessed_at": timestamp,
                "status": "active",
                "confidence": 0.8,
                "activation_base": 1.0,
                "activation": 1.0,
                "importance": 0.5,
                "scope": fixture.scope,
                "canonical_attribute": fixture.canonical_attribute,
                "embedding_dense": pack_vector([1.0]),
                "embedding_model": "activation-ab",
                "embedding_dim": 1,
            }
        )
    connection.commit()
    return database


def _is_access_day(fixture: ClaimFixture, day: int) -> bool:
    if not fixture.access_every_days or day == 0:
        return False
    if fixture.final_access_day is not None and day > fixture.final_access_day:
        return False
    return day % fixture.access_every_days == 0


def run(output_dir: Path, replay_days: int) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    work = output_dir / "activation_arms_work"
    work.mkdir(parents=True, exist_ok=True)
    settings = Settings.for_test()
    rows: list[dict[str, Any]] = []
    arm_reports: dict[str, Any] = {}
    for arm in ARMS:
        database_path = work / f"{arm}.db"
        if database_path.exists():
            database_path.unlink()
        database = _build_database(database_path)
        connection = database.open()
        archive_days: dict[str, int | None] = {fixture.claim_id: None for fixture in FIXTURES}
        initial_confidence = {
            str(row["id"]): float(row["confidence"]) for row in connection.execute("SELECT id,confidence FROM claims")
        }
        for day in range(replay_days + 1):
            now = START + timedelta(days=day)
            for fixture in FIXTURES:
                if _is_access_day(fixture, day):
                    connection.execute(
                        "UPDATE claims SET last_accessed_at=?,access_count=access_count+1 WHERE id=? AND status='active'",
                        (now.isoformat(), fixture.claim_id),
                    )
            connection.commit()
            outcome = decay_claims(connection, now.isoformat(), **_decay_kwargs(settings, arm))
            snapshots = {
                str(row["id"]): dict(row)
                for row in connection.execute(
                    "SELECT id,status,confidence,activation,last_accessed_at,access_count FROM claims"
                )
            }
            for fixture in FIXTURES:
                snapshot = snapshots[fixture.claim_id]
                if snapshot["status"] == "archived" and archive_days[fixture.claim_id] is None:
                    archive_days[fixture.claim_id] = day
                rows.append(
                    {
                        "arm": arm,
                        "day": day,
                        "now": now.isoformat(),
                        "claim_id": fixture.claim_id,
                        "label": fixture.label,
                        "scope": fixture.scope,
                        "status": snapshot["status"],
                        "confidence": snapshot["confidence"],
                        "activation": snapshot["activation"],
                        "last_accessed_at": snapshot["last_accessed_at"],
                        "access_count": snapshot["access_count"],
                        "worker_archived": outcome["archived"],
                    }
                )
        final_rows = {
            str(row["id"]): dict(row)
            for row in connection.execute("SELECT id,status,confidence,activation FROM claims")
        }
        must_keep = [fixture for fixture in FIXTURES if fixture.label == "must_keep"]
        protected_keep = [
            fixture
            for fixture in must_keep
            if fixture.scope == "permanent" or fixture.canonical_attribute.startswith("identity.")
        ]
        temporal_cleanup = [
            fixture for fixture in FIXTURES if fixture.label == "must_archive" and fixture.scope == "temporal"
        ]
        confidence_changes = [
            fixture.claim_id
            for fixture in FIXTURES
            if abs(float(final_rows[fixture.claim_id]["confidence"]) - initial_confidence[fixture.claim_id]) > 1e-12
        ]
        arm_reports[arm] = {
            "archive_days": archive_days,
            "identity_permanent_false_archive_rate": sum(
                final_rows[fixture.claim_id]["status"] == "archived" for fixture in protected_keep
            )
            / len(protected_keep),
            "all_must_keep_false_archive_rate": sum(
                final_rows[fixture.claim_id]["status"] == "archived" for fixture in must_keep
            )
            / len(must_keep),
            "temporal_cleanup_efficiency": sum(
                final_rows[fixture.claim_id]["status"] == "archived" for fixture in temporal_cleanup
            )
            / len(temporal_cleanup),
            "all_must_archive_cleanup_efficiency": sum(
                final_rows[fixture.claim_id]["status"] == "archived"
                for fixture in FIXTURES
                if fixture.label == "must_archive"
            )
            / sum(fixture.label == "must_archive" for fixture in FIXTURES),
            "confidence_changed_claims": confidence_changes,
            "confidence_separation_violations": (len(confidence_changes) if arm == "activation_halflife" else None),
            "final": final_rows,
        }
        database.close()
    activation = arm_reports["activation_halflife"]
    confidence_arm = arm_reports["confidence_halflife"]
    gates = {
        "activation_zero_identity_permanent_harm": activation["identity_permanent_false_archive_rate"] == 0.0,
        "activation_zero_confidence_changes": activation["confidence_separation_violations"] == 0,
        "activation_cleans_all_temporal_targets": activation["temporal_cleanup_efficiency"] == 1.0,
        "confidence_halflife_changes_confidence": bool(confidence_arm["confidence_changed_claims"]),
    }
    recommendation = "activation_halflife" if all(gates.values()) else "legacy_linear"
    report = {
        "schema_version": "activation-arms-report-v1",
        "replay_days": replay_days,
        "fixture": [asdict(item) for item in FIXTURES],
        "decay_parameters": _decay_kwargs(settings, "activation_halflife"),
        "arms": arm_reports,
        "gates": gates,
        "recommendation": recommendation,
        "confidence_halflife_disposition": (
            "reject" if confidence_arm["confidence_changed_claims"] else "inconclusive"
        ),
    }
    (output_dir / "activation_arms_runs.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    (output_dir / "activation_arms_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("var/eval"))
    parser.add_argument("--replay-days", type=int, default=540)
    args = parser.parse_args()
    if args.replay_days < 1:
        parser.error("--replay-days must be positive")
    report = run(args.output_dir, args.replay_days)
    summary = {
        "recommendation": report["recommendation"],
        "confidence_halflife_disposition": report["confidence_halflife_disposition"],
        "gates": report["gates"],
        "arms": {
            arm: {
                "archive_days": values["archive_days"],
                "identity_permanent_false_archive_rate": values["identity_permanent_false_archive_rate"],
                "temporal_cleanup_efficiency": values["temporal_cleanup_efficiency"],
                "confidence_changed_claims": values["confidence_changed_claims"],
            }
            for arm, values in report["arms"].items()
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
