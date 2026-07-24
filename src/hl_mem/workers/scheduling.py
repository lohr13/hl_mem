"""后台任务的公共日调度逻辑。"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from typing import Any

from hl_mem.storage.jobs import JobRepository


def enqueue_daily_job(
    connection: sqlite3.Connection,
    now: str,
    schedule: dict[str, str | int],
    job_type: str,
    payload: dict[str, Any],
    env_name: str,
) -> str | None:
    """检查日调度配置，构造幂等 job。命中返回 job_id，未命中返回 None。"""
    try:
        if "scheduled_minutes" in schedule:
            scheduled_minutes = int(schedule["scheduled_minutes"])
        else:
            hour_text, minute_text = str(schedule["cron"]).split(":", 1)
            scheduled_minutes = int(hour_text) * 60 + int(minute_text)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"{env_name} must use HH:MM format") from error
    if not 0 <= scheduled_minutes < 24 * 60:
        raise ValueError(f"{env_name} must use HH:MM format")

    current = datetime.fromisoformat(now.replace("Z", "+00:00"))
    if current.hour * 60 + current.minute < scheduled_minutes:
        return None

    job_id = uuid.uuid4().hex
    idempotency_prefix = str(schedule.get("idempotency_prefix") or job_type)
    created = JobRepository(connection).insert_job(
        {
            "id": job_id,
            "job_type": job_type,
            "payload": payload,
            "idempotency_key": f"{idempotency_prefix}:{current.date().isoformat()}",
            "created_at": now,
            "updated_at": now,
        }
    )
    connection.commit()
    return job_id if created else None
