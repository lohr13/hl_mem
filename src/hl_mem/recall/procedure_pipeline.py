"""Tool/Procedure intent 的 Experience 专用候选召回与排序。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from hl_mem.domain.temporal import RecallIntent, parse_utc
from hl_mem.storage._shared import escape_like_pattern
from hl_mem.storage.experience import ExperienceRepository

MemoryKind = Literal["policy", "episode", "trace", "claim"]


@dataclass(frozen=True)
class MemoryCandidate:
    """统一表示可进入 Tool/Procedure 上下文的记忆候选。"""

    memory_type: MemoryKind
    memory_id: str
    text: str
    score: float
    evidence: tuple[dict[str, object], ...]
    features: dict[str, float]


def _tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[\w\u4e00-\u9fff]+", text.casefold()) if token}


def _match(query: str, text: str) -> float:
    query_tokens = _tokens(query)
    if not query_tokens:
        return 0.0
    lowered = text.casefold()
    if query.casefold() in lowered:
        return 1.0
    return min(1.0, sum(token in lowered for token in query_tokens) / len(query_tokens))


def _bounded(value: object, default: float = 0.0) -> float:
    if not isinstance(value, (str, bytes, int, float)):
        return default
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return default


def _recency(value: object, half_life_days: int) -> float:
    if not value:
        return 0.0
    age = max(
        0.0,
        (datetime.now(timezone.utc) - parse_utc(str(value))).total_seconds() / 86400,
    )
    return float(0.5 ** (age / half_life_days))


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def recall_procedure(
    repository: ExperienceRepository,
    query: str,
    intent: RecallIntent,
    namespace: str,
    limit: int,
    *,
    candidate_limit: int = 30,
    recent_outcome_window: int = 20,
    outcome_half_life_days: int = 30,
    claim_candidates: list[MemoryCandidate] | None = None,
) -> list[MemoryCandidate]:
    """召回并按类型内公式排序 Experience 候选，失败 Episode 不作为步骤推荐。"""
    policies = repository.list_active_policies(namespace, query, candidate_limit)
    episodes = repository.list_success_episodes(namespace, query, candidate_limit)
    traces = repository.list_traces_for_episodes(
        [str(item["id"]) for item in episodes],
        candidate_limit,
        query,
    )
    result: list[MemoryCandidate] = []
    normalized_query = query.strip()
    pattern = f"%{escape_like_pattern(normalized_query)}%"
    outcome_filter = (
        "AND (goal LIKE ? ESCAPE '\\' OR COALESCE(outcome_summary,'') LIKE ? ESCAPE '\\') " if normalized_query else ""
    )
    outcome_parameters: tuple[object, ...] = (
        (namespace, pattern, pattern, recent_outcome_window) if normalized_query else (namespace, recent_outcome_window)
    )
    outcome_rows = repository.connection.execute(
        "SELECT status,COALESCE(ended_at,started_at) AS occurred_at FROM episodes "
        f"WHERE namespace_key=? {outcome_filter}"
        "ORDER BY COALESCE(ended_at,started_at) DESC,id ASC LIMIT ?",
        outcome_parameters,
    ).fetchall()
    outcome_weight = 0.0
    success_weight = 0.0
    for row in outcome_rows:
        weight = _recency(row["occurred_at"], outcome_half_life_days)
        outcome_weight += weight
        success_weight += weight if row["status"] == "success" else 0.0
    recent_outcome = success_weight / outcome_weight if outcome_weight else 0.5
    policy_evidence: dict[str, list[dict[str, object]]] = {}
    policy_ids = [str(item["id"]) for item in policies]
    if policy_ids:
        placeholders = ",".join("?" for _ in policy_ids)
        rows = repository.connection.execute(
            f"SELECT derived_id,evidence_type,evidence_id,relation,weight FROM evidence_links "
            f"WHERE derived_type='policy' AND derived_id IN ({placeholders})",
            policy_ids,
        ).fetchall()
        for row in rows:
            policy_evidence.setdefault(str(row["derived_id"]), []).append(
                {
                    "type": str(row["evidence_type"]),
                    "id": str(row["evidence_id"]),
                    "relation": str(row["relation"]),
                    "weight": float(row["weight"]),
                }
            )
    for item in policies:
        text = f"{item['trigger']}\n{_text(item['procedure'])}"
        features = {
            "text_match": _match(query, text),
            "reliability": _bounded(item.get("reliability")),
            "usefulness": _bounded(item.get("usefulness_score"), 0.5),
            "recency": _recency(item.get("updated_at"), outcome_half_life_days),
        }
        score = (
            0.40 * features["text_match"]
            + 0.35 * features["reliability"]
            + 0.15 * features["usefulness"]
            + 0.10 * features["recency"]
        )
        result.append(
            MemoryCandidate(
                "policy",
                str(item["id"]),
                text,
                score,
                tuple(policy_evidence.get(str(item["id"])) or ({"type": "policy", "id": str(item["id"])},)),
                features,
            )
        )
    for item in episodes:
        text = f"{item['goal']}\n{item.get('outcome_summary') or ''}"
        features = {
            "text_match": _match(query, text),
            "reward": _bounded(item.get("reward")),
            "recent_outcome": recent_outcome,
            "recency": _recency(item.get("ended_at") or item.get("started_at"), outcome_half_life_days),
        }
        score = (
            0.35 * features["text_match"]
            + 0.30 * features["reward"]
            + 0.20 * features["recent_outcome"]
            + 0.15 * features["recency"]
        )
        result.append(
            MemoryCandidate(
                "episode",
                str(item["id"]),
                text,
                score,
                ({"type": "episode", "id": str(item["id"]), "outcome": "success"},),
                features,
            )
        )
    for item in traces:
        text = f"{item['action']}\n{item.get('observation') or ''}"
        features = {
            "action_match": _match(query, str(item["action"])),
            "parent_reward": _bounded(item.get("parent_reward")),
            "value": _bounded(item.get("value")),
            "recent_outcome": recent_outcome,
        }
        score = (
            0.40 * features["action_match"]
            + 0.25 * features["parent_reward"]
            + 0.20 * features["value"]
            + 0.15 * features["recent_outcome"]
        )
        result.append(
            MemoryCandidate(
                "trace",
                str(item["id"]),
                text,
                score,
                (
                    {"type": "trace", "id": str(item["id"])},
                    {
                        "type": "episode",
                        "id": str(item["episode_id"]),
                        "relation": "parent",
                    },
                ),
                features,
            )
        )
    result.extend(claim_candidates or [])
    type_order = {"policy": 0, "episode": 1, "trace": 2, "claim": 3}
    result.sort(key=lambda item: (type_order[item.memory_type], -item.score, item.memory_id))
    return result[: max(limit, 1) * 4]
