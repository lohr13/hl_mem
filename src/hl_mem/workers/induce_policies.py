"""从近期成功 Episode 中归纳可复用策略。"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from hl_mem.experience.service import ExperienceService
from hl_mem.settings import Settings
from hl_mem.storage.repository import JobRepository


def induce_policies(
    connection: Any,
    now: str,
    lookback_days: int | None = None,
    min_episodes: int | None = None,
) -> dict[str, int]:
    """按任务类型和工具序列聚类最近七天的高奖励 Episode。"""
    current = datetime.fromisoformat(now.replace("Z", "+00:00"))
    defaults = Settings()
    effective_lookback = lookback_days or defaults.policy_induction_lookback_days
    effective_min_episodes = min_episodes or defaults.policy_induction_min_episodes
    cutoff = (current - timedelta(days=effective_lookback)).isoformat()
    rows = connection.execute(
        "SELECT id,goal,scope_json FROM episodes "
        "WHERE status='success' AND reward>=0.5 "
        "AND coalesce(ended_at,started_at)>=? AND coalesce(ended_at,started_at)<=? "
        "ORDER BY started_at,id",
        (cutoff, now),
    ).fetchall()
    clusters: dict[tuple[str, tuple[str, ...]], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        scope = json.loads(row["scope_json"] or "{}")
        task_type = str(scope.get("task_type") or "general")
        actions = tuple(
            trace["action"]
            for trace in connection.execute(
                "SELECT action FROM traces WHERE episode_id=? ORDER BY sequence_no LIMIT 5", (row["id"],)
            ).fetchall()
        )
        if actions:
            # 用前3个action作为聚类key，而非完整序列
            prefix = actions[:3]
            clusters[(task_type, prefix)].append(dict(row))

    service = ExperienceService(connection, min_support=2)
    induced = 0
    eligible = 0
    for (task_type, actions), episodes in clusters.items():
        if len(episodes) < effective_min_episodes:
            continue
        eligible += 1
        trigger = f"{task_type} {' '.join(actions)}"
        if connection.execute(
            "SELECT 1 FROM policies WHERE namespace_key='default' AND trigger=?", (trigger,)
        ).fetchone():
            continue
        service.induce_policy(trigger, {"steps": list(actions)}, [item["id"] for item in episodes], now)
        induced += 1
    return {"clusters": eligible, "policies_induced": induced}


def enqueue_daily_policy_induction(connection: Any, now: str, cron: str) -> bool:
    """到达每日计划时间后幂等创建策略归纳任务。"""
    try:
        hour_text, minute_text = cron.split(":", 1)
        scheduled_minutes = int(hour_text) * 60 + int(minute_text)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("HL_MEM_INDUCE_POLICIES_CRON must use HH:MM format") from error
    current = datetime.fromisoformat(now.replace("Z", "+00:00"))
    if not 0 <= scheduled_minutes < 24 * 60:
        raise ValueError("HL_MEM_INDUCE_POLICIES_CRON must use HH:MM format")
    if current.hour * 60 + current.minute < scheduled_minutes:
        return False
    return JobRepository(connection).insert_job(
        {
            "id": uuid.uuid4().hex,
            "job_type": "induce_policies",
            "payload_json": "{}",
            "idempotency_key": f"induce_policies:{current.date().isoformat()}",
            "created_at": now,
            "updated_at": now,
        }
    )
