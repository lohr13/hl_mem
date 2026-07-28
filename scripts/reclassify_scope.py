"""按当前 scope 规范规则清洗历史 permanent claim。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from hl_mem.domain.claims.attributes import validate_slot_instance
from hl_mem.domain.claims.retention import TTLPolicy, compute_expiration
from hl_mem.ingest.llm_extractor import normalize_scope
from hl_mem.settings import Settings

RULES_VERSION = "normalize-scope-v1"
QUOTED_REPORT_RE = re.compile(r"(?i)(?:\[quoted message\]|quoted report|历史报告|引用消息)")
REASON_PRIORITY = {
    "slot_short_ttl": 0,
    "health_check": 1,
    "runtime_configuration": 2,
    "quoted_report": 3,
    "tool_snapshot": 4,
    "explicit_temporal_signal": 5,
}


@dataclass(frozen=True)
class ScopeChange:
    """单条 claim 的待执行 scope 变更。"""

    claim_id: str
    reason_code: str
    value_preview: str
    expires_at: str | None
    expiration_reason: str


@dataclass(frozen=True)
class ScopePlan:
    """由 dry-run 生成、可在 apply 前复核的不可变计划。"""

    rules_version: str
    generated_at: str
    claim_count: int
    active_count: int
    active_permanent_count: int
    changes: tuple[ScopeChange, ...]
    claims_state_token: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _retention_policy_from_env() -> TTLPolicy:
    """仅加载本脚本依赖的 TTL 环境变量，避免触发无关组件配置校验。"""
    defaults = TTLPolicy()
    policy = TTLPolicy(
        temporal_ttl_days_low=int(os.getenv("HL_MEM_TEMPORAL_TTL_DAYS_LOW", str(defaults.temporal_ttl_days_low))),
        temporal_ttl_days_normal=int(
            os.getenv("HL_MEM_TEMPORAL_TTL_DAYS_NORMAL", str(defaults.temporal_ttl_days_normal))
        ),
        temporal_ttl_days_high=int(os.getenv("HL_MEM_TEMPORAL_TTL_DAYS_HIGH", str(defaults.temporal_ttl_days_high))),
        importance_low_threshold=float(
            os.getenv("HL_MEM_IMPORTANCE_LOW_THRESHOLD", str(defaults.importance_low_threshold))
        ),
        importance_high_threshold=float(
            os.getenv("HL_MEM_IMPORTANCE_HIGH_THRESHOLD", str(defaults.importance_high_threshold))
        ),
        importance_write_floor=float(os.getenv("HL_MEM_IMPORTANCE_WRITE_FLOOR", str(defaults.importance_write_floor))),
        slot_short_ttl_seconds=int(os.getenv("HL_MEM_SLOT_SHORT_TTL_SECONDS", str(defaults.slot_short_ttl_seconds))),
        short_ttl_slots=defaults.short_ttl_slots,
    )
    if (
        min(
            policy.temporal_ttl_days_low,
            policy.temporal_ttl_days_normal,
            policy.temporal_ttl_days_high,
            policy.slot_short_ttl_seconds,
        )
        <= 0
    ):
        raise ValueError("TTL durations must be positive")
    if not 0 <= policy.importance_low_threshold <= policy.importance_high_threshold <= 1:
        raise ValueError("importance thresholds must satisfy 0 <= low <= high <= 1")
    return policy


def _decode_json(value: str | None, fallback: Any) -> Any:
    if value is None:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


def _value_preview(value: Any) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return re.sub(r"\s+", " ", text).strip()[:80]


def _claims_state_token(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    cursor = connection.execute("SELECT * FROM claims ORDER BY id")
    for row in cursor:
        digest.update(json.dumps(tuple(row), ensure_ascii=False, default=str).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _event_contexts(connection: sqlite3.Connection, claim_id: str) -> list[dict[str, str]]:
    rows = connection.execute(
        "SELECT e.actor_type,e.event_type,e.content_json "
        "FROM evidence_links AS link "
        "JOIN events AS e ON e.id=link.evidence_id "
        "WHERE link.derived_type='claim' AND link.derived_id=? AND link.evidence_type='event' "
        "ORDER BY e.recorded_at,e.id",
        (claim_id,),
    ).fetchall()
    contexts: list[dict[str, str]] = []
    for row in rows:
        content = _decode_json(row["content_json"], {})
        text = content.get("text", "") if isinstance(content, dict) else str(content)
        contexts.append(
            {
                "actor_type": str(row["actor_type"] or ""),
                "event_type": str(row["event_type"] or ""),
                "source_kind": "quoted_report" if QUOTED_REPORT_RE.search(str(text)) else "",
            }
        )
    return contexts or [{"actor_type": "", "event_type": "", "source_kind": ""}]


def _classification(row: sqlite3.Row, connection: sqlite3.Connection) -> tuple[str, str]:
    value = _decode_json(row["value_json"], row["value_json"])
    qualifiers = _decode_json(row["qualifiers_json"], {})
    if not isinstance(qualifiers, dict):
        qualifiers = {}
    results = [
        normalize_scope(
            "permanent",
            str(row["predicate"] or ""),
            row["canonical_slot"],
            str(row["subject_entity_id"] or ""),
            value,
            qualifiers,
            canonical_attribute=row["canonical_attribute"],
            actor_type=context["actor_type"],
            event_type=context["event_type"],
            source_kind=context["source_kind"],
        )
        for context in _event_contexts(connection, str(row["id"]))
    ]
    temporal_reasons = [reason for scope, reason in results if scope == "temporal"]
    if not temporal_reasons:
        return "permanent", results[0][1]
    reason = min(temporal_reasons, key=lambda item: (REASON_PRIORITY.get(item, 99), item))
    return "temporal", reason


def _expiration(row: sqlite3.Row, policy: TTLPolicy) -> tuple[str | None, str]:
    observed_at = str(row["observed_at"] or "")
    recorded_from = str(row["recorded_from"] or "")
    if not observed_at and not recorded_from:
        raise ValueError(f"claim {row['id']} has no expiration anchor")
    qualifiers = _decode_json(row["qualifiers_json"], {})
    if not isinstance(qualifiers, dict):
        qualifiers = {}
    return compute_expiration(
        scope="temporal",
        importance=float(row["importance"] if row["importance"] is not None else 0.5),
        volatility=str(row["volatility"] or "stable"),
        canonical_slot=validate_slot_instance(row["canonical_slot"], qualifiers),
        valid_to=row["valid_to"],
        observed_at=observed_at,
        recorded_from=recorded_from,
        policy=policy,
    )


def build_plan(connection: sqlite3.Connection, policy: TTLPolicy, *, generated_at: str | None = None) -> ScopePlan:
    """读取 active permanent claim 并生成降级计划，不写数据库。"""
    connection.row_factory = sqlite3.Row
    counts = connection.execute(
        "SELECT count(*) AS total,"
        "sum(status='active') AS active,"
        "sum(status='active' AND scope='permanent') AS active_permanent "
        "FROM claims"
    ).fetchone()
    rows = connection.execute("SELECT * FROM claims WHERE status='active' AND scope='permanent' ORDER BY id").fetchall()
    changes: list[ScopeChange] = []
    for row in rows:
        normalized_scope, reason_code = _classification(row, connection)
        if normalized_scope != "temporal":
            continue
        expires_at, expiration_reason = _expiration(row, policy)
        value = _decode_json(row["value_json"], row["value_json"])
        changes.append(
            ScopeChange(
                claim_id=str(row["id"]),
                reason_code=reason_code,
                value_preview=_value_preview(value),
                expires_at=expires_at,
                expiration_reason=expiration_reason,
            )
        )
    return ScopePlan(
        rules_version=RULES_VERSION,
        generated_at=generated_at or _now_iso(),
        claim_count=int(counts["total"] or 0),
        active_count=int(counts["active"] or 0),
        active_permanent_count=int(counts["active_permanent"] or 0),
        changes=tuple(changes),
        claims_state_token=_claims_state_token(connection),
    )


def write_markdown_report(path: Path, plan: ScopePlan) -> None:
    """写出便于人工核对的 dry-run Markdown 报告。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Scope Reclassification Dry Run",
        "",
        f"- Rules version: `{plan.rules_version}`",
        f"- Generated at: `{plan.generated_at}`",
        f"- Claims: {plan.claim_count}",
        f"- Active claims: {plan.active_count}",
        f"- Active permanent claims scanned: {plan.active_permanent_count}",
        f"- Pending downgrades: **{len(plan.changes)}**",
        "",
        "| # | Claim ID | Reason code | New expires_at | Value preview (80 chars) |",
        "|---:|---|---|---|---|",
    ]
    for index, change in enumerate(plan.changes, start=1):
        preview = change.value_preview.replace("|", r"\|")
        lines.append(
            f"| {index} | `{change.claim_id}` | `{change.reason_code}` | "
            f"`{change.expires_at or ''}` ({change.expiration_reason}) | {preview} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _audit_row(change: ScopeChange, occurred_at: str, trace_id: str) -> tuple[Any, ...]:
    detail = json.dumps(
        {
            "old_scope": "permanent",
            "new_scope": "temporal",
            "reason_code": change.reason_code,
            "expires_at": change.expires_at,
            "expiration_reason": change.expiration_reason,
            "rules_version": RULES_VERSION,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return (
        occurred_at,
        "governance",
        "scope_reclassified",
        "changed",
        trace_id,
        "default",
        change.claim_id,
        detail,
    )


def apply_plan(connection: sqlite3.Connection, policy: TTLPolicy, expected_plan: ScopePlan) -> int:
    """事务性重算计划、更新 scope/TTL，并写入逐条 audit log。"""
    connection.row_factory = sqlite3.Row
    connection.execute("BEGIN IMMEDIATE")
    try:
        current_plan = build_plan(connection, policy, generated_at=expected_plan.generated_at)
        if current_plan != expected_plan:
            raise RuntimeError("database changed since dry-run; apply aborted")
        occurred_at = _now_iso()
        trace_id = uuid.uuid4().hex
        for change in expected_plan.changes:
            cursor = connection.execute(
                "UPDATE claims SET scope='temporal',expires_at=? "
                "WHERE id=? AND status='active' AND scope='permanent'",
                (change.expires_at, change.claim_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"claim changed during apply: {change.claim_id}")
            connection.execute(
                "INSERT INTO audit_log("
                "occurred_at,phase,action,outcome,trace_id,tenant_id,claim_id,detail_json"
                ") VALUES (?,?,?,?,?,?,?,?)",
                _audit_row(change, occurred_at, trace_id),
            )
        connection.commit()
        return len(expected_plan.changes)
    except Exception:
        connection.rollback()
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path(Settings().database_path))
    parser.add_argument("--report", type=Path, default=Path("docs/SCOPE-RECLASSIFY-DRYRUN.md"))
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--apply", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """执行 dry-run 或实际清洗。"""
    args = _parser().parse_args(argv)
    policy = _retention_policy_from_env()
    if args.dry_run:
        uri = f"file:{args.database.resolve().as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            plan = build_plan(connection, policy)
        finally:
            connection.close()
        write_markdown_report(args.report, plan)
        print(f"pending_downgrades={len(plan.changes)}")
        print(f"report={args.report}")
        return 0

    connection = sqlite3.connect(args.database)
    try:
        expected_plan = build_plan(connection, policy)
        updated = apply_plan(connection, policy, expected_plan)
    finally:
        connection.close()
    print(f"updated={updated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
