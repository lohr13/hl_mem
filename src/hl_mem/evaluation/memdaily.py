"""MemDaily JSON 到 hl_mem BenchmarkCase 的确定性转换器。

MemDaily 是一个中文长期记忆评测数据集，包含 6 种题型：
simple / conditional / comparative / aggregative / post_processing / noisy。
每种题型下有若干子类型（events, roles, items, places, hybrid）。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hl_mem.evaluation.models import BenchmarkCase, GoldTemporal, LifecycleCheckpoint

QUESTION_TYPES: tuple[str, ...] = (
    "simple",
    "conditional",
    "comparative",
    "aggregative",
    "post_processing",
    "noisy",
)

FALLBACK_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)


def parse_memdaily_timestamp(text: str | None) -> str:
    """将 '2024年04月01日 周一 08:30' 格式解析为 ISO 8601 字符串。

    无法解析时返回 fallback epoch。
    """
    if not text or not text.strip():
        return FALLBACK_EPOCH.isoformat()
    raw = text.strip()
    # 期望格式: "2024年04月01日 周一 08:30"
    # 先提取年月日和时分
    try:
        date_part, _, time_part = raw.partition(" ")
        # date_part = "2024年04月01日", 中间还可能有 "周一" 等
        # 找最后的 time_part: 期望 "HH:MM" 格式
        # 将 raw 拆分: 年月日 + 星期 + 时分
        # 安全策略：用正则提取
        import re

        match = re.match(
            r"(\d{4})年(\d{2})月(\d{2})日\s+\S+\s+(\d{2}):(\d{2})",
            raw,
        )
        if match:
            year, month, day, hour, minute = (
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
                int(match.group(4)),
                int(match.group(5)),
            )
            return datetime(year, month, day, hour, minute, tzinfo=timezone.utc).isoformat()
    except (ValueError, TypeError):
        pass
    return FALLBACK_EPOCH.isoformat()


class MemDailyAdapter:
    """MemDaily 数据集到 BenchmarkCase 的转换器。

    每条轨迹（trajectory）包含一个 message_list 和一个 QA。
    适配器把 message_list 转为 hl_mem 事件，把 QA 转为评测约束。
    """

    VERSION = "1"

    def load(self, source: Path, subset: str = "events") -> Iterable[BenchmarkCase]:
        """加载 MemDaily JSON 并按指定子集生成 BenchmarkCase。

        Args:
            source: memdaily.json 路径。
            subset: 子集选择。格式为 ``qtype`` 或 ``qtype:subtype``。
                当 qtype 为 ``all`` 时遍历所有题型；默认 subtype 为 events。
                特殊值 ``all:events`` 遍历所有题型的 events 子集。
        """
        raw = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            raise ValueError("MemDaily source must be a JSON object keyed by question type")

        qtypes: list[str]
        subtype: str

        if subset.startswith("all"):
            parts = subset.split(":", 1)
            subtype = parts[1] if len(parts) > 1 else "events"
            qtypes = list(QUESTION_TYPES)
        else:
            parts = subset.split(":", 1)
            qtype = parts[0]
            subtype = parts[1] if len(parts) > 1 else "events"
            if qtype not in raw:
                raise ValueError(f"MemDaily question type {qtype!r} not found in source")
            qtypes = [qtype]

        for qtype in qtypes:
            type_data = raw.get(qtype)
            if not isinstance(type_data, Mapping):
                continue
            subtype_data = type_data.get(subtype)
            if not isinstance(subtype_data, Sequence) or isinstance(subtype_data, (str, bytes)):
                continue
            for trajectory in subtype_data:
                if not isinstance(trajectory, Mapping):
                    continue
                yield self._convert_case(qtype, subtype, trajectory)

    def _convert_case(self, qtype: str, subtype: str, trajectory: Mapping[str, Any]) -> BenchmarkCase:
        """将一条 MemDaily 轨迹转为 BenchmarkCase。"""
        tid = str(trajectory.get("tid", 0))
        case_id = f"memdaily:{qtype}:{subtype}:{tid}"
        namespace = f"eval:memdaily:{qtype}:{subtype}:{tid}"

        message_list = trajectory.get("message_list") or []
        if not isinstance(message_list, Sequence) or isinstance(message_list, (str, bytes)):
            raise ValueError(f"MemDaily trajectory {case_id}: message_list must be a list")

        events: list[dict[str, object]] = []
        mid_to_event_id: dict[int, str] = {}
        raw_by_mid: dict[int, Mapping[str, Any]] = {}

        for index, message in enumerate(message_list):
            if not isinstance(message, Mapping):
                continue
            mid = int(message.get("mid", index))
            text = str(message.get("message") or message.get("content") or "")
            time_str = str(message.get("time") or "")
            place = str(message.get("place") or "")
            occurred_at = parse_memdaily_timestamp(time_str)

            event_id = f"memdaily:{qtype}:{subtype}:{tid}:mid:{mid}"
            idempotency_key = f"memdaily:{case_id}:mid:{mid}"

            content: dict[str, object] = {
                "text": text,
                "benchmark_locator": {
                    "case_id": case_id,
                    "mid": mid,
                    "place": place,
                    "time": time_str,
                },
            }

            events.append(
                {
                    "id": event_id,
                    "idempotency_key": idempotency_key,
                    "tenant_id": namespace,
                    "event_type": "message",
                    "actor_type": "user",
                    "content": content,
                    "occurred_at": occurred_at,
                    "recorded_at": occurred_at,
                }
            )
            mid_to_event_id[mid] = event_id
            raw_by_mid[mid] = message

        qa = trajectory.get("QA") or {}
        if not isinstance(qa, Mapping):
            qa = {}

        question = str(qa.get("question") or "")
        gold_answer = str(qa.get("answer") or "").strip() or None
        qa_time = str(qa.get("time") or "").strip()
        as_of = parse_memdaily_timestamp(qa_time) if qa_time else None

        target_step_ids = qa.get("target_step_id") or []
        if not isinstance(target_step_ids, Sequence) or isinstance(target_step_ids, (str, bytes)):
            target_step_ids = []
        gold_evidence_event_ids = tuple(
            dict.fromkeys(mid_to_event_id[int(mid)] for mid in target_step_ids if int(mid) in mid_to_event_id)
        )

        gold_temporal: tuple[GoldTemporal, ...] = ()
        temporal_seen: set[str] = set()
        for event_id in gold_evidence_event_ids:
            if event_id in temporal_seen:
                continue
            temporal_seen.add(event_id)
            # 反查原始消息的 mid
            mid_for_event = next((mid for mid, eid in mid_to_event_id.items() if eid == event_id), None)
            raw_msg = raw_by_mid.get(mid_for_event, {}) if mid_for_event is not None else {}
            occurred = parse_memdaily_timestamp(str(raw_msg.get("time") or ""))
            gold_temporal_item = GoldTemporal(
                evidence_event_id=event_id,
                occurred_start=occurred,
                occurred_end=None,
                valid_from=occurred,
                valid_to=None,
            )
            gold_temporal = gold_temporal + (gold_temporal_item,)

        # 生命周期检查点：所有事件在最后一条消息时可见
        checkpoints: tuple[LifecycleCheckpoint, ...] = ()
        if events:
            latest = max(str(event["occurred_at"]) for event in events)
            checkpoints = (
                LifecycleCheckpoint(
                    at=latest,
                    known_as_of=None,
                    expected_visible_event_ids=tuple(str(event["id"]) for event in events),
                    expected_hidden_event_ids=(),
                    expected_status_by_event_id={},
                    worker_action=None,
                ),
            )

        return BenchmarkCase(
            case_id=case_id,
            events=tuple(events),
            query=question,
            gold_evidence_event_ids=gold_evidence_event_ids,
            gold_temporal=gold_temporal,
            lifecycle_checkpoints=checkpoints,
            gold_answer=gold_answer,
            as_of=as_of,
            known_as_of=None,
            category=f"{qtype}:{subtype}",
        )

    @staticmethod
    def case_namespace(qtype: str, subtype: str, tid: int | str) -> str:
        """返回某条轨迹的 tenant_id / namespace。"""
        return f"eval:memdaily:{qtype}:{subtype}:{tid}"
