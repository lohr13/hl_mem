"""按方案 1 恢复被旧 conflict key 误标的 disputed claim。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


RULES_VERSION = "scheme-1-status-only-v1"
LIVE_STATUSES = frozenset({"active", "candidate", "disputed"})


@dataclass(frozen=True)
class CleanupPlan:
    """dry-run 生成的不可变清理计划。"""

    rules_version: str
    generated_at: str
    disputed_count: int
    eligible_ids: tuple[str, ...]
    skipped_reasons: dict[str, int]
    state_token: str
    evidence_token: str
    evidence_count: int


@dataclass(frozen=True)
class CleanupResult:
    """apply 后的验证结果。"""

    updated_count: int
    active_count: int
    disputed_count: int
    integrity_check: str
    foreign_key_violations: int
    evidence_unchanged: bool


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_current(row: sqlite3.Row, now: datetime) -> bool:
    valid_to = _parse_time(row["valid_to"])
    expires_at = _parse_time(row["expires_at"])
    recorded_to = _parse_time(row["recorded_to"])
    return all(bound is None or bound > now for bound in (valid_to, expires_at, recorded_to))


def _query_token(connection: sqlite3.Connection, query: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    for row in connection.execute(query):
        digest.update(json.dumps(tuple(row), ensure_ascii=False, default=str).encode("utf-8"))
        digest.update(b"\n")
        count += 1
    return digest.hexdigest(), count


def _claim_state_token(connection: sqlite3.Connection) -> str:
    token, _ = _query_token(
        connection,
        "SELECT id,namespace_key,conflict_key,conflict_key_version,status,valid_to,expires_at,recorded_to "
        "FROM claims ORDER BY id",
    )
    return token


def _evidence_token(connection: sqlite3.Connection) -> tuple[str, int]:
    return _query_token(connection, "SELECT * FROM evidence_links ORDER BY id")


def build_plan(connection: sqlite3.Connection, *, now: str | None = None) -> CleanupPlan:
    """生成 status-only 清理计划，不写入数据库。"""
    connection.row_factory = sqlite3.Row
    generated_at = now or _now_iso()
    current_time = _parse_time(generated_at)
    if current_time is None:
        raise ValueError("now must be a valid timestamp")
    rows = list(
        connection.execute(
            "SELECT id,namespace_key,conflict_key,conflict_key_version,status,valid_to,expires_at,recorded_to "
            "FROM claims ORDER BY id"
        )
    )
    disputed = [row for row in rows if row["status"] == "disputed"]
    eligible: list[str] = []
    skipped = {"expired": 0, "live_peer": 0}
    for claim in disputed:
        if claim["conflict_key_version"] != 2 or not _is_current(claim, current_time):
            skipped["expired"] += 1
            continue
        has_live_peer = any(
            peer["id"] != claim["id"]
            and peer["namespace_key"] == claim["namespace_key"]
            and peer["conflict_key"] == claim["conflict_key"]
            and peer["status"] in LIVE_STATUSES
            and _is_current(peer, current_time)
            for peer in rows
        )
        if has_live_peer:
            skipped["live_peer"] += 1
        else:
            eligible.append(str(claim["id"]))
    evidence_token, evidence_count = _evidence_token(connection)
    return CleanupPlan(
        rules_version=RULES_VERSION,
        generated_at=generated_at,
        disputed_count=len(disputed),
        eligible_ids=tuple(eligible),
        skipped_reasons=skipped,
        state_token=_claim_state_token(connection),
        evidence_token=evidence_token,
        evidence_count=evidence_count,
    )


def _backup_database(connection: sqlite3.Connection, backup_path: Path) -> None:
    if backup_path.exists():
        raise FileExistsError(f"backup already exists: {backup_path}")
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup = sqlite3.connect(backup_path)
    try:
        connection.backup(backup)
    finally:
        backup.close()


def apply_cleanup(database_path: Path, backup_path: Path, expected_plan: CleanupPlan) -> CleanupResult:
    """备份数据库并事务性应用已核对的 dry-run 计划。"""
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        _backup_database(connection, backup_path)
        connection.execute("BEGIN IMMEDIATE")
        current_plan = build_plan(connection, now=expected_plan.generated_at)
        if current_plan != expected_plan:
            raise RuntimeError("database changed since dry-run; apply aborted")
        for claim_id in expected_plan.eligible_ids:
            cursor = connection.execute(
                "UPDATE claims SET status='active' WHERE id=? AND status='disputed'",
                (claim_id,),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"claim changed during apply: {claim_id}")
        evidence_token, evidence_count = _evidence_token(connection)
        if (evidence_token, evidence_count) != (
            expected_plan.evidence_token,
            expected_plan.evidence_count,
        ):
            raise RuntimeError("evidence_links changed during apply")
        integrity_check = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        if integrity_check != "ok" or foreign_key_violations:
            raise RuntimeError("database integrity verification failed")
        active_count = int(connection.execute("SELECT count(*) FROM claims WHERE status='active'").fetchone()[0])
        disputed_count = int(
            connection.execute("SELECT count(*) FROM claims WHERE status='disputed'").fetchone()[0]
        )
        connection.commit()
        return CleanupResult(
            updated_count=len(expected_plan.eligible_ids),
            active_count=active_count,
            disputed_count=disputed_count,
            integrity_check=integrity_check,
            foreign_key_violations=foreign_key_violations,
            evidence_unchanged=True,
        )
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def write_report(report_path: Path, plan: CleanupPlan, result: CleanupResult | None) -> None:
    """写入不包含 claim value 的 JSON 运维报告。"""
    report_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"plan": asdict(plan), "result": asdict(result) if result else None}
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--report-path", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-path", type=Path)
    parser.add_argument("--plan-path", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """执行 dry-run 或 apply 命令。"""
    args = _parser().parse_args(argv)
    if args.dry_run:
        connection = sqlite3.connect(f"file:{args.database.resolve().as_posix()}?mode=ro", uri=True)
        try:
            plan = build_plan(connection)
        finally:
            connection.close()
        write_report(args.report_path, plan, result=None)
        print(json.dumps(asdict(plan), ensure_ascii=False, indent=2))
        return 0
    if args.backup_path is None or args.plan_path is None:
        _parser().error("--apply requires --backup-path and --plan-path")
    raw_plan = json.loads(args.plan_path.read_text(encoding="utf-8"))["plan"]
    raw_plan["eligible_ids"] = tuple(raw_plan["eligible_ids"])
    plan = CleanupPlan(**raw_plan)
    result = apply_cleanup(args.database, args.backup_path, plan)
    write_report(args.report_path, plan, result)
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
