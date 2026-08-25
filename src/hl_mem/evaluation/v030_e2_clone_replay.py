"""Read-only-source E2 clone application, evidence, invariant and rollback replay."""

from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Mapping

from hl_mem.domain.claims.dedup import compute_dedup_pair_key
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.workers.deduplicate import POLICY_VERSION, _apply_equivalent_pair, rollback_dedup_action


def prepare_export_values(table: str, source: Mapping[str, Any], columns: set[str]) -> dict[str, Any]:
    """Adapt the bounded legacy export to additive required columns in the current schema."""

    values = {key: value for key, value in source.items() if key in columns}
    if table == "dedup_pairs":
        values.setdefault(
            "pair_key", compute_dedup_pair_key(str(source["left_claim_id"]), str(source["right_claim_id"]))
        )
        values.setdefault("namespace_key", "default")
        values.setdefault("created_at", source.get("reviewed_at") or "1970-01-01T00:00:00+00:00")
    return values


def _merge_volcano(connection: sqlite3.Connection, path: str | Path) -> dict[str, int]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))

    def insert_rows(table: str, rows: list[dict[str, Any]]) -> int:
        columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
        inserted = 0
        for source in rows:
            values = prepare_export_values(table, source, columns)
            if table == "claims":
                # The bounded Volcano export omits lifecycle targets outside its 30-claim slice.
                # Those links are not E2 inputs, so the clone adapter drops only the dangling refs.
                values["supersedes_id"] = None
                values["superseded_by_id"] = None
                raw_embedding = values.get("embedding_dense")
                if isinstance(raw_embedding, str) and raw_embedding.startswith("b'"):
                    try:
                        values["embedding_dense"] = ast.literal_eval(raw_embedding)
                    except (SyntaxError, ValueError):
                        values["embedding_dense"] = None
            names = sorted(values)
            cursor = connection.execute(
                f"INSERT OR IGNORE INTO {table}({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
                tuple(values[name] for name in names),
            )
            inserted += cursor.rowcount
        return inserted

    claims = insert_rows("claims", list(payload["claims"]))
    pairs = insert_rows("dedup_pairs", list(payload["pairs"]))
    connection.commit()
    pair_ids = [str(row["id"]) for row in payload["pairs"]]
    present = connection.execute(
        f"SELECT count(*) FROM dedup_pairs WHERE id IN ({','.join('?' for _ in pair_ids)})", pair_ids
    ).fetchone()[0]
    return {
        "volcano_claims_merged": claims,
        "volcano_pairs_merged": pairs,
        "volcano_pairs_present_after_merge": int(present),
    }


def _state_fingerprint(connection: sqlite3.Connection, claim_ids: list[str]) -> str:
    claims = [
        dict(row)
        for row in connection.execute(
            f"SELECT id,status,valid_to,recorded_to,superseded_by_id,value_json FROM claims "
            f"WHERE id IN ({','.join('?' for _ in claim_ids)}) ORDER BY id",
            claim_ids,
        )
    ]
    evidence = [
        tuple(row)
        for row in connection.execute(
            f"SELECT id,derived_id,evidence_type,evidence_id,relation,weight FROM evidence_links "
            f"WHERE derived_type='claim' AND derived_id IN ({','.join('?' for _ in claim_ids)}) ORDER BY id",
            claim_ids,
        )
    ]
    placeholders = ",".join("?" for _ in claim_ids)
    conflicts = [
        tuple(row)
        for row in connection.execute(
            f"SELECT id,status,left_claim_id,right_claim_id FROM conflict_cases "
            f"WHERE left_claim_id IN ({placeholders}) OR right_claim_id IN ({placeholders}) ORDER BY id",
            [*claim_ids, *claim_ids],
        )
    ]
    members = [
        tuple(row)
        for row in connection.execute(
            f"SELECT case_id,candidate_key,claim_id FROM conflict_candidate_members "
            f"WHERE claim_id IN ({placeholders}) ORDER BY case_id,candidate_key,claim_id",
            claim_ids,
        )
    ]
    raw = json.dumps(
        {"claims": claims, "evidence": evidence, "conflicts": conflicts, "members": members},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _active_conflict_collisions(connection: sqlite3.Connection) -> int:
    return int(
        connection.execute(
            "SELECT count(*) FROM (SELECT namespace_key,conflict_key FROM claims "
            "WHERE status='active' AND conflict_key IS NOT NULL GROUP BY namespace_key,conflict_key HAVING count(*)>1)"
        ).fetchone()[0]
    )


def run_e2_clone_rehearsal(
    database_path: str | Path,
    volcano_path: str | Path,
    preregistered: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply disjoint source-active pairs through the real service seam, then roll all back."""

    with tempfile.TemporaryDirectory(prefix="hl-mem-e2-v2-") as temporary:
        clone_path = Path(temporary) / "e2-clone.sqlite3"
        source = sqlite3.connect(f"file:{Path(database_path).as_posix()}?mode=ro", uri=True)
        target = sqlite3.connect(clone_path)
        try:
            source.backup(target)
        finally:
            source.close()
            target.close()
        database = Database(clone_path)
        connection = database.open()
        try:
            merged = _merge_volcano(connection, volcano_path)
            repository = ClaimRepository(connection)
            selected: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
            used: set[str] = set()
            for case in preregistered.get("cases") or []:
                payload = case.get("input") or {}
                claims = payload.get("claims") or []
                ids = [str(claim["id"]) for claim in claims]
                current = repository.batch_get_claims(ids)
                if (
                    payload.get("historical_decision") != "equivalent"
                    or not bool((payload.get("hard_validator") or {}).get("safe"))
                    or float(payload.get("judge_confidence") or 0) < 0.98
                    or len(current) != 2
                    or any(current[claim_id]["status"] != "active" for claim_id in ids)
                    or used.intersection(ids)
                ):
                    continue
                selected.append((str(payload["pair_id"]), current[ids[0]], current[ids[1]]))
                used.update(ids)
            claim_ids = sorted(used)
            before_fingerprint = _state_fingerprint(connection, claim_ids)
            collisions_before = _active_conflict_collisions(connection)
            applied: list[dict[str, Any]] = []
            evidence_ok = 0
            for index, (pair_id, left, right) in enumerate(selected):
                connection.execute(
                    "UPDATE dedup_pairs SET decision='equivalent',judge_confidence=0.99,judge_model='e2-v2-blind',"
                    "judge_reason='blind_replay',policy_version=?,applied_at=NULL,candidate_strategy='legacy_no_slot',"
                    "entity_proof_id=NULL,auto_apply_eligible=1 WHERE id=?",
                    (POLICY_VERSION, pair_id),
                )
                connection.commit()
                evidence_by_claim = {
                    claim["id"]: {
                        str(row[0])
                        for row in connection.execute(
                            "SELECT evidence_id FROM evidence_links WHERE derived_type='claim' AND derived_id=?",
                            (claim["id"],),
                        )
                    }
                    for claim in (left, right)
                }
                if not _apply_equivalent_pair(
                    connection,
                    pair_id,
                    left,
                    right,
                    f"2026-08-25T12:{index // 60:02d}:{index % 60:02d}+00:00",
                    0.98,
                ):
                    continue
                action = connection.execute(
                    "SELECT id,after_json FROM governance_actions WHERE domain='dedup' AND subject_ref=? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (pair_id,),
                ).fetchone()
                after = json.loads(str(action["after_json"]))
                survivor = next(claim_id for claim_id, state in after["claims"].items() if state["status"] == "active")
                losing = next(claim_id for claim_id in after["claims"] if claim_id != survivor)
                survivor_evidence = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT evidence_id FROM evidence_links WHERE derived_type='claim' AND derived_id=?",
                        (survivor,),
                    )
                }
                evidence_ok += int(evidence_by_claim[losing] <= survivor_evidence)
                applied.append({"pair_id": pair_id, "action_id": str(action["id"])})
            collisions_applied = _active_conflict_collisions(connection)
            for item in reversed(applied):
                rollback_dedup_action(
                    connection,
                    item["action_id"],
                    rolled_back_at="2026-08-25T13:00:00+00:00",
                    reason="E2 v2 frozen clone rollback",
                )
            after_fingerprint = _state_fingerprint(connection, claim_ids)
            foreign_key_errors = len(connection.execute("PRAGMA foreign_key_check").fetchall())
            return {
                **merged,
                "selected_disjoint_source_active_pairs": len(selected),
                "applied_actions": len(applied),
                "evidence_closure_rate": evidence_ok / len(applied) if applied else 0.0,
                "rollback_reversible": float(bool(applied) and before_fingerprint == after_fingerprint),
                "before_fingerprint": before_fingerprint,
                "after_rollback_fingerprint": after_fingerprint,
                "conflict_collisions_before": collisions_before,
                "conflict_collisions_applied": collisions_applied,
                "conflict_invariant_preserved": collisions_applied <= collisions_before,
                "foreign_key_errors": foreign_key_errors,
            }
        finally:
            database.close()


def attach_recall_comparison(
    rehearsal: Mapping[str, Any],
    baseline_metrics: Mapping[str, float],
    current_metrics: Mapping[str, float],
    *,
    paired_regression_p: float,
) -> dict[str, Any]:
    keys = ("recall_at_5", "recall_at_10", "mrr")
    deltas = {key: float(current_metrics[key]) - float(baseline_metrics[key]) for key in keys}
    payload = {
        "baseline": dict(baseline_metrics),
        "current": dict(current_metrics),
        "delta": deltas,
        "paired_regression_p": paired_regression_p,
        "contract": "batch0_frozen_smoke_v2",
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return {
        **dict(rehearsal),
        "recall_absolute_drop": max((max(0.0, -value) for value in deltas.values()), default=0.0),
        "recall_regression_p": paired_regression_p,
        "recall_metrics_sha256": hashlib.sha256(encoded).hexdigest(),
        "recall_comparison": payload,
    }
