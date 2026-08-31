"""跨进程原子的 Provider 用量预留、结算与恢复。"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import cast

from hl_mem.errors import UsageLimitExceededError, UsageReservationError
from hl_mem.observability.usage_types import (
    _LABEL_PATTERN,
    _MODEL_PATTERN,
    UsageAmount,
    UsageIdentity,
    UsageLimits,
    UsageReservation,
    default_usage_ledger_path,
)
from hl_mem.plugins.contracts import ProviderCapability

USAGE_LEDGER_SCHEMA_VERSION = 1


class UsageGovernor:
    def __init__(
        self,
        path: str | Path,
        limits: UsageLimits,
        *,
        lease_seconds: int = 300,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise ValueError("lease_seconds must be a positive integer")
        self.path = Path(path)
        self.limits = limits
        self.lease_seconds = lease_seconds
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _clock(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            raise ValueError("usage clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > USAGE_LEDGER_SCHEMA_VERSION:
                raise UsageReservationError(
                    f"usage ledger schema {version} is newer than supported version {USAGE_LEDGER_SCHEMA_VERSION}"
                )
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS usage_reservations ("
                "id TEXT PRIMARY KEY, usage_date TEXT NOT NULL, capability TEXT NOT NULL, "
                "operation TEXT NOT NULL, plugin_id TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, "
                "reserved_requests INTEGER NOT NULL, reserved_input_tokens INTEGER NOT NULL, "
                "reserved_output_tokens INTEGER NOT NULL, reserved_embedding_items INTEGER NOT NULL, "
                "reserved_rerank_documents INTEGER NOT NULL, reserved_images INTEGER NOT NULL, "
                "reserved_cost_microunits INTEGER, attempts INTEGER NOT NULL DEFAULT 0, "
                "lease_expires_at TEXT NOT NULL, state TEXT NOT NULL, final_signature TEXT, "
                "created_at TEXT NOT NULL, finalized_at TEXT)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_reservations_active "
                "ON usage_reservations(usage_date,state,lease_expires_at)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS usage_events ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, reservation_id TEXT NOT NULL UNIQUE, "
                "usage_date TEXT NOT NULL, capability TEXT NOT NULL, operation TEXT NOT NULL, "
                "plugin_id TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, "
                "requests INTEGER NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, "
                "embedding_items INTEGER NOT NULL, rerank_documents INTEGER NOT NULL, images INTEGER NOT NULL, "
                "cost_microunits INTEGER, status TEXT NOT NULL, latency_ms REAL NOT NULL, error_class TEXT, "
                "attempts INTEGER NOT NULL, unknown_outcome INTEGER NOT NULL, unknown_cost INTEGER NOT NULL, "
                "created_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_events_date_capability " "ON usage_events(usage_date,capability)"
            )
            self._import_legacy(connection)
            connection.execute(f"PRAGMA user_version={USAGE_LEDGER_SCHEMA_VERSION}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _import_legacy(self, connection: sqlite3.Connection) -> None:
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='token_budget'").fetchone()
        if exists is None:
            return
        created_at = self._clock().isoformat()
        for row in connection.execute("SELECT budget_date,used_tokens FROM token_budget"):
            usage_date, used_tokens = str(row[0]), int(row[1])
            connection.execute(
                "INSERT OR IGNORE INTO usage_events ("
                "reservation_id,usage_date,capability,operation,plugin_id,provider,model,"
                "requests,input_tokens,output_tokens,embedding_items,rerank_documents,images,cost_microunits,"
                "status,latency_ms,error_class,attempts,unknown_outcome,unknown_cost,created_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"legacy_worker_budget:{usage_date}",
                    usage_date,
                    ProviderCapability.LLM.value,
                    "legacy_worker_budget",
                    "hl-mem.builtin",
                    "legacy",
                    "legacy",
                    0,
                    used_tokens,
                    0,
                    0,
                    0,
                    0,
                    None,
                    "imported",
                    0.0,
                    None,
                    0,
                    0,
                    1,
                    created_at,
                ),
            )

    @staticmethod
    def _amount_values(amount: UsageAmount) -> tuple[int, int, int, int, int, int, int | None]:
        return (
            amount.requests,
            amount.input_tokens,
            amount.output_tokens,
            amount.embedding_items,
            amount.rerank_documents,
            amount.images,
            amount.cost_microunits,
        )

    @staticmethod
    def _reserved_amount(row: sqlite3.Row) -> UsageAmount:
        return UsageAmount(
            requests=int(row["reserved_requests"]),
            input_tokens=int(row["reserved_input_tokens"]),
            output_tokens=int(row["reserved_output_tokens"]),
            embedding_items=int(row["reserved_embedding_items"]),
            rerank_documents=int(row["reserved_rerank_documents"]),
            images=int(row["reserved_images"]),
            cost_microunits=(
                int(row["reserved_cost_microunits"]) if row["reserved_cost_microunits"] is not None else None
            ),
        )

    @staticmethod
    def _aggregate(connection: sqlite3.Connection, table: str, day: str) -> sqlite3.Row:
        if table == "usage_events":
            row = connection.execute(
                "SELECT COALESCE(SUM(requests),0) requests, COALESCE(SUM(input_tokens),0) input_tokens, "
                "COALESCE(SUM(output_tokens),0) output_tokens, "
                "COALESCE(SUM(embedding_items),0) embedding_items, "
                "COALESCE(SUM(rerank_documents),0) rerank_documents, COALESCE(SUM(images),0) images, "
                "COALESCE(SUM(cost_microunits),0) cost_microunits, "
                "COALESCE(SUM(CASE WHEN cost_microunits IS NULL THEN 1 ELSE 0 END),0) null_costs, "
                "COALESCE(SUM(unknown_cost),0) unknown_costs "
                "FROM usage_events WHERE usage_date=?",
                (day,),
            ).fetchone()
            return cast(sqlite3.Row, row)
        row = connection.execute(
            "SELECT COALESCE(SUM(reserved_requests),0) requests, "
            "COALESCE(SUM(reserved_input_tokens),0) input_tokens, "
            "COALESCE(SUM(reserved_output_tokens),0) output_tokens, "
            "COALESCE(SUM(reserved_embedding_items),0) embedding_items, "
            "COALESCE(SUM(reserved_rerank_documents),0) rerank_documents, "
            "COALESCE(SUM(reserved_images),0) images, "
            "COALESCE(SUM(reserved_cost_microunits),0) cost_microunits, "
            "COALESCE(SUM(CASE WHEN reserved_cost_microunits IS NULL THEN 1 ELSE 0 END),0) null_costs, "
            "0 unknown_costs FROM usage_reservations WHERE usage_date=? AND state='active'",
            (day,),
        ).fetchone()
        return cast(sqlite3.Row, row)

    def reserve(self, identity: UsageIdentity, estimate: UsageAmount) -> UsageReservation:
        now = self._clock()
        day = now.date().isoformat()
        lease_expires_at = now + timedelta(seconds=self.lease_seconds)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            settled = self._aggregate(connection, "usage_events", day)
            reserved = self._aggregate(connection, "usage_reservations", day)
            proposed_requests = int(settled["requests"]) + int(reserved["requests"]) + estimate.requests
            proposed_tokens = (
                int(settled["input_tokens"])
                + int(settled["output_tokens"])
                + int(reserved["input_tokens"])
                + int(reserved["output_tokens"])
                + estimate.total_tokens
            )
            if self.limits.daily_requests > 0 and proposed_requests > self.limits.daily_requests:
                raise UsageLimitExceededError("daily Provider request limit would be exceeded")
            if self.limits.daily_tokens > 0 and proposed_tokens > self.limits.daily_tokens:
                raise UsageLimitExceededError("daily Provider token limit would be exceeded")
            if self.limits.daily_cost_microunits > 0:
                if estimate.cost_microunits is None:
                    raise UsageLimitExceededError("finite daily cost limit requires a cost estimate")
                if int(settled["unknown_costs"]) > 0 or int(reserved["null_costs"]) > 0:
                    raise UsageLimitExceededError("daily Provider cost is unknown; finite cost budget is closed")
                proposed_cost = (
                    int(settled["cost_microunits"]) + int(reserved["cost_microunits"]) + estimate.cost_microunits
                )
                if proposed_cost > self.limits.daily_cost_microunits:
                    raise UsageLimitExceededError("daily Provider cost limit would be exceeded")
            reservation_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO usage_reservations ("
                "id,usage_date,capability,operation,plugin_id,provider,model,"
                "reserved_requests,reserved_input_tokens,reserved_output_tokens,reserved_embedding_items,"
                "reserved_rerank_documents,reserved_images,reserved_cost_microunits,attempts,lease_expires_at,"
                "state,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    reservation_id,
                    day,
                    identity.capability.value,
                    identity.operation,
                    identity.plugin_id,
                    identity.provider,
                    identity.model,
                    *self._amount_values(estimate),
                    0,
                    lease_expires_at.isoformat(),
                    "active",
                    now.isoformat(),
                ),
            )
            connection.commit()
            return UsageReservation(reservation_id, estimate, lease_expires_at)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_attempt(self, reservation_id: str) -> int:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state,attempts FROM usage_reservations WHERE id=?", (reservation_id,)
            ).fetchone()
            if row is None:
                raise UsageReservationError(f"unknown usage reservation {reservation_id!r}")
            if row["state"] != "active":
                raise UsageReservationError(f"usage reservation {reservation_id!r} is already finalized")
            attempts = int(row["attempts"]) + 1
            lease_expires_at = self._clock() + timedelta(seconds=self.lease_seconds)
            connection.execute(
                "UPDATE usage_reservations SET attempts=?,lease_expires_at=? WHERE id=?",
                (attempts, lease_expires_at.isoformat(), reservation_id),
            )
            connection.commit()
            return attempts
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _signature(
        kind: str,
        amount: UsageAmount | None,
        status: str,
        latency_ms: float,
        error_class: str | None,
        reason: str | None = None,
    ) -> str:
        return json.dumps(
            {
                "kind": kind,
                "amount": None if amount is None else UsageGovernor._amount_values(amount),
                "status": status,
                "latency_ms": latency_ms,
                "error_class": error_class,
                "reason": reason,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _check_existing_finalization(row: sqlite3.Row, signature: str) -> bool:
        if row["state"] == "active":
            return False
        if row["final_signature"] == signature:
            return True
        raise UsageReservationError(f"contradictory finalization for usage reservation {row['id']!r}")

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        actual: UsageAmount,
        *,
        status: str,
        latency_ms: float,
        error_class: str | None,
        unknown_outcome: bool,
    ) -> None:
        reserved = self._reserved_amount(row)
        stored_cost = actual.cost_microunits
        unknown_cost = stored_cost is None
        if stored_cost is None:
            stored_cost = reserved.cost_microunits
            if stored_cost is not None and reserved.requests > 0:
                accounted_requests = actual.requests or int(row["attempts"])
                accounted_requests = min(accounted_requests, reserved.requests)
                stored_cost = math.ceil(stored_cost * accounted_requests / reserved.requests)
        connection.execute(
            "INSERT INTO usage_events ("
            "reservation_id,usage_date,capability,operation,plugin_id,provider,model,"
            "requests,input_tokens,output_tokens,embedding_items,rerank_documents,images,cost_microunits,"
            "status,latency_ms,error_class,attempts,unknown_outcome,unknown_cost,created_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["id"],
                row["usage_date"],
                row["capability"],
                row["operation"],
                row["plugin_id"],
                row["provider"],
                row["model"],
                *self._amount_values(actual)[:-1],
                stored_cost,
                status[:64],
                float(latency_ms),
                error_class[:128] if error_class is not None else None,
                int(row["attempts"]),
                int(unknown_outcome),
                int(unknown_cost),
                self._clock().isoformat(),
            ),
        )

    def _settle(
        self,
        reservation_id: str,
        actual: UsageAmount | None,
        *,
        kind: str,
        status: str,
        latency_ms: float,
        error_class: str | None,
    ) -> None:
        if not math.isfinite(latency_ms) or latency_ms < 0:
            raise ValueError("latency_ms must be finite and non-negative")
        if _LABEL_PATTERN.fullmatch(status) is None:
            raise ValueError("usage status must be a bounded low-cardinality label")
        if error_class is not None and _MODEL_PATTERN.fullmatch(error_class) is None:
            raise ValueError("usage error_class must be a bounded low-cardinality label")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM usage_reservations WHERE id=?", (reservation_id,)).fetchone()
            if row is None:
                raise UsageReservationError(f"unknown usage reservation {reservation_id!r}")
            resolved_actual = self._reserved_amount(row) if actual is None else actual
            signature = self._signature(kind, resolved_actual, status, latency_ms, error_class)
            if self._check_existing_finalization(row, signature):
                connection.rollback()
                return
            self._insert_event(
                connection,
                row,
                resolved_actual,
                status=status,
                latency_ms=latency_ms,
                error_class=error_class,
                unknown_outcome=kind == "unknown",
            )
            connection.execute(
                "UPDATE usage_reservations SET state='settled',final_signature=?,finalized_at=? WHERE id=?",
                (signature, self._clock().isoformat(), reservation_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def settle(
        self,
        reservation_id: str,
        actual: UsageAmount,
        *,
        status: str,
        latency_ms: float,
        error_class: str | None = None,
    ) -> None:
        self._settle(
            reservation_id,
            actual,
            kind="settled",
            status=status,
            latency_ms=latency_ms,
            error_class=error_class,
        )

    def settle_unknown(
        self,
        reservation_id: str,
        actual: UsageAmount | None = None,
        *,
        status: str,
        latency_ms: float,
        error_class: str | None,
    ) -> None:
        self._settle(
            reservation_id,
            actual,
            kind="unknown",
            status=status,
            latency_ms=latency_ms,
            error_class=error_class,
        )

    def release(self, reservation_id: str, *, reason: str) -> None:
        if _LABEL_PATTERN.fullmatch(reason) is None:
            raise ValueError("usage release reason must be a bounded low-cardinality label")
        signature = self._signature("released", None, "released", 0.0, None, reason)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM usage_reservations WHERE id=?", (reservation_id,)).fetchone()
            if row is None:
                raise UsageReservationError(f"unknown usage reservation {reservation_id!r}")
            if self._check_existing_finalization(row, signature):
                connection.rollback()
                return
            if int(row["attempts"]) > 0:
                raise UsageReservationError("cannot release a usage reservation after an attempt was sent")
            connection.execute(
                "UPDATE usage_reservations SET state='released',final_signature=?,finalized_at=? WHERE id=?",
                (signature, self._clock().isoformat(), reservation_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def recover_expired(self) -> dict[str, int]:
        now = self._clock()
        released = 0
        settled_unknown = 0
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM usage_reservations WHERE state='active' AND lease_expires_at<? ORDER BY id",
                (now.isoformat(),),
            ).fetchall()
            for row in rows:
                if int(row["attempts"]) == 0:
                    signature = self._signature("released", None, "released", 0.0, None, "lease_expired")
                    connection.execute(
                        "UPDATE usage_reservations SET state='released',final_signature=?,finalized_at=? WHERE id=?",
                        (signature, now.isoformat(), row["id"]),
                    )
                    released += 1
                    continue
                actual = self._reserved_amount(row)
                signature = self._signature("unknown", actual, "unknown", 0.0, "LeaseExpired")
                self._insert_event(
                    connection,
                    row,
                    actual,
                    status="unknown",
                    latency_ms=0.0,
                    error_class="LeaseExpired",
                    unknown_outcome=True,
                )
                connection.execute(
                    "UPDATE usage_reservations SET state='settled',final_signature=?,finalized_at=? WHERE id=?",
                    (signature, now.isoformat(), row["id"]),
                )
                settled_unknown += 1
            connection.commit()
            return {"released": released, "settled_unknown": settled_unknown}
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _snapshot_amount(row: sqlite3.Row, *, unknown_cost: bool) -> dict[str, int | None]:
        input_tokens = int(row["input_tokens"])
        output_tokens = int(row["output_tokens"])
        return {
            "requests": int(row["requests"]),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "embedding_items": int(row["embedding_items"]),
            "rerank_documents": int(row["rerank_documents"]),
            "images": int(row["images"]),
            "cost_microunits": None if unknown_cost else int(row["cost_microunits"]),
        }

    def snapshot(self, day: str | date | None = None) -> dict[str, object]:
        if day is None:
            selected_day = self._clock().date().isoformat()
        elif isinstance(day, date):
            selected_day = day.isoformat()
        else:
            selected_day = date.fromisoformat(day).isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            settled = self._aggregate(connection, "usage_events", selected_day)
            reserved = self._aggregate(connection, "usage_reservations", selected_day)
            counts = {
                str(row["capability"]): int(row["requests"])
                for row in connection.execute(
                    "SELECT capability,COALESCE(SUM(requests),0) requests FROM usage_events "
                    "WHERE usage_date=? GROUP BY capability ORDER BY capability",
                    (selected_day,),
                )
            }
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        unknown_cost_count = int(settled["unknown_costs"])
        reserved_unknown_cost = int(reserved["null_costs"]) > 0
        spent_requests = int(settled["requests"]) + int(reserved["requests"])
        spent_tokens = (
            int(settled["input_tokens"])
            + int(settled["output_tokens"])
            + int(reserved["input_tokens"])
            + int(reserved["output_tokens"])
        )
        spent_cost = int(settled["cost_microunits"]) + int(reserved["cost_microunits"])
        remaining = {
            "requests": (
                -1 if self.limits.daily_requests <= 0 else max(0, self.limits.daily_requests - spent_requests)
            ),
            "tokens": -1 if self.limits.daily_tokens <= 0 else max(0, self.limits.daily_tokens - spent_tokens),
            "cost_microunits": (
                -1
                if self.limits.daily_cost_microunits <= 0
                else (
                    0
                    if unknown_cost_count or reserved_unknown_cost
                    else max(0, self.limits.daily_cost_microunits - spent_cost)
                )
            ),
        }
        return {
            "date": selected_day,
            "settled": self._snapshot_amount(settled, unknown_cost=unknown_cost_count > 0),
            "reserved": self._snapshot_amount(reserved, unknown_cost=reserved_unknown_cost),
            "remaining": remaining,
            "unknown_cost_count": unknown_cost_count,
            "counts_by_capability": counts,
        }


__all__ = [
    "USAGE_LEDGER_SCHEMA_VERSION",
    "UsageAmount",
    "UsageGovernor",
    "UsageIdentity",
    "UsageLimits",
    "UsageReservation",
    "default_usage_ledger_path",
]
