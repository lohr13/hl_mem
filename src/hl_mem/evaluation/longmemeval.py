"""LongMemEval JSON 到 hl_mem benchmark case 的确定性转换器。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from hl_mem.evaluation.models import BenchmarkCase, GoldTemporal, LifecycleCheckpoint


class LongMemEvalAdapter:
    """兼容 LongMemEval 会话 JSON 与本地小型 fixture 的转换器。"""

    VERSION = "1"
    FALLBACK_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)

    def __init__(self, manifest_path: Path | None = None) -> None:
        self.manifest_path = manifest_path or Path(__file__).parents[3] / "evaluation" / "longmemeval" / "manifest.json"

    @classmethod
    def from_fixture(cls, source: Path | None = None) -> Iterable[BenchmarkCase]:
        """加载仓库内不依赖网络和真实模型的小型 fixture。"""
        fixture = source or Path(__file__).parents[3] / "tests" / "fixtures" / "longmemeval_small.json"
        return cls().load(fixture, "all")

    def load(self, source: Path, subset: str) -> Iterable[BenchmarkCase]:
        """加载 JSON，按 manifest 子集筛选并转换为稳定 case。"""
        if subset != "all":
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            expected_hash = manifest.get("source_sha256")
            if expected_hash:
                actual_hash = hashlib.sha256(source.read_bytes()).hexdigest()
                if actual_hash != expected_hash:
                    raise ValueError(
                        f"LongMemEval source SHA-256 mismatch: expected {expected_hash}, got {actual_hash}"
                    )
        raw = json.loads(source.read_text(encoding="utf-8"))
        records = raw.get("data", raw) if isinstance(raw, Mapping) else raw
        if not isinstance(records, list):
            raise ValueError("LongMemEval source must be a JSON list or an object with a data list")
        allowed = self._subset_ids(subset)
        seen: set[str] = set()
        for record in records:
            if not isinstance(record, Mapping):
                raise ValueError("LongMemEval cases must be JSON objects")
            case_id = str(record.get("question_id") or record.get("case_id") or record.get("id") or "")
            if not case_id:
                raise ValueError("LongMemEval case is missing question_id/case_id")
            if case_id in seen:
                raise ValueError(f"duplicate LongMemEval case id: {case_id}")
            seen.add(case_id)
            if allowed is not None and case_id not in allowed:
                continue
            yield self._convert_case(case_id, record)

    def _subset_ids(self, subset: str) -> set[str] | None:
        if subset == "all":
            return None
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        subsets = manifest.get("subsets", {})
        if subset not in subsets:
            raise ValueError(f"unknown LongMemEval subset: {subset}")
        definition = subsets[subset]
        ids = definition.get("ids", definition) if isinstance(definition, Mapping) else definition
        if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
            raise ValueError(f"manifest subset {subset!r} must contain an ids list")
        if len(ids) != len(set(ids)):
            raise ValueError(f"manifest subset {subset!r} contains duplicate ids")
        return set(ids)

    def _convert_case(self, case_id: str, record: Mapping[str, Any]) -> BenchmarkCase:
        messages = self._messages(record)
        events: list[dict[str, object]] = []
        id_map: dict[str, str] = {}
        raw_by_id: dict[str, Mapping[str, Any]] = {}
        for index, message in enumerate(messages):
            message_id = str(message.get("message_id") or message.get("id") or index)
            session_key = str(message["_session_key"])
            scoped_message_id = f"{session_key}:{message_id}"
            stable_id = f"lme:{case_id}:{scoped_message_id}"
            occurred_at = self._timestamp(message) or (self.FALLBACK_EPOCH + timedelta(seconds=index)).isoformat()
            role = str(message.get("role") or message.get("speaker") or "user").lower()
            actor_type = {"human": "user", "ai": "assistant"}.get(role, role)
            if actor_type not in {"user", "assistant", "system", "tool"}:
                actor_type = "user"
            events.append(
                {
                    "id": stable_id,
                    "idempotency_key": f"longmemeval:{case_id}:{scoped_message_id}",
                    "tenant_id": f"eval:{case_id}",
                    "event_type": "message",
                    "actor_type": actor_type,
                    "content": {
                        "text": str(message.get("content") or message.get("text") or ""),
                        "benchmark_locator": {
                            "case_id": case_id,
                            "session_id": session_key,
                            "message_id": message_id,
                        },
                    },
                    "occurred_at": occurred_at,
                    "recorded_at": (self.FALLBACK_EPOCH + timedelta(seconds=index)).isoformat(),
                }
            )
            id_map[scoped_message_id] = stable_id
            raw_by_id[scoped_message_id] = message
        stable_ids = [str(event["id"]) for event in events]
        if len(stable_ids) != len(set(stable_ids)):
            raise ValueError(f"duplicate stable message id in LongMemEval case: {case_id}")
        message_counts: dict[str, int] = {}
        for message in messages:
            message_id = str(message.get("message_id") or message.get("id"))
            message_counts[message_id] = message_counts.get(message_id, 0) + 1
        for scoped_message_id, stable_id in list(id_map.items()):
            message_id = scoped_message_id.split(":", 1)[1]
            if message_counts.get(message_id) == 1:
                id_map[message_id] = stable_id
                raw_by_id[message_id] = raw_by_id[scoped_message_id]
        answer_ids = self._answer_ids(record)
        gold_ids = tuple(dict.fromkeys(id_map[item] for item in answer_ids if item in id_map))
        gold_temporal_items: list[GoldTemporal] = []
        temporal_seen: set[str] = set()
        for message_id, event_id in id_map.items():
            if event_id not in gold_ids or event_id in temporal_seen:
                continue
            temporal_seen.add(event_id)
            raw_message = raw_by_id[message_id]
            gold_temporal_items.append(
                GoldTemporal(
                    evidence_event_id=event_id,
                    occurred_start=self._timestamp(raw_message),
                    occurred_end=None,
                    valid_from=self._timestamp(raw_message),
                    valid_to=self._optional_string(raw_message.get("valid_to")),
                )
            )
        gold_temporal = tuple(gold_temporal_items)
        checkpoints = self._checkpoints(events, messages, id_map)
        return BenchmarkCase(
            case_id=case_id,
            events=tuple(events),
            query=str(record.get("question") or record.get("query") or ""),
            gold_evidence_event_ids=gold_ids,
            gold_temporal=gold_temporal,
            lifecycle_checkpoints=checkpoints,
            gold_answer=self._optional_string(record.get("answer") or record.get("gold_answer")),
            as_of=self._optional_string(record.get("as_of")),
            known_as_of=self._optional_string(record.get("known_as_of")),
            category=str(record.get("category") or record.get("question_type") or "uncategorized"),
        )

    @staticmethod
    def _messages(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        sessions = record.get("haystack_sessions") or record.get("sessions") or record.get("messages") or []
        if not isinstance(sessions, Sequence) or isinstance(sessions, (str, bytes)):
            raise ValueError("LongMemEval sessions/messages must be a list")
        flattened: list[Mapping[str, Any]] = []
        for session_index, session in enumerate(sessions):
            session_key = (
                str(session.get("session_id") or session.get("id") or session_index)
                if isinstance(session, Mapping)
                else str(session_index)
            )
            items = session.get("messages", []) if isinstance(session, Mapping) else session
            if isinstance(items, Mapping):
                flattened.append({**items, "_session_key": session_key})
            elif isinstance(items, Sequence) and not isinstance(items, (str, bytes)):
                flattened.extend({**item, "_session_key": session_key} for item in items if isinstance(item, Mapping))
        return flattened

    @staticmethod
    def _answer_ids(record: Mapping[str, Any]) -> tuple[str, ...]:
        value = (
            record.get("answer_message_ids") or record.get("gold_message_ids") or record.get("answer_session_ids") or ()
        )
        if isinstance(value, (str, int)):
            return (str(value),)
        return tuple(str(item) for item in value) if isinstance(value, Sequence) else ()

    @staticmethod
    def _timestamp(message: Mapping[str, Any]) -> str | None:
        return LongMemEvalAdapter._optional_string(
            message.get("timestamp") or message.get("date") or message.get("occurred_at")
        )

    @staticmethod
    def _optional_string(value: object) -> str | None:
        return str(value) if value is not None and str(value) else None

    def _checkpoints(
        self,
        events: Sequence[Mapping[str, object]],
        messages: Sequence[Mapping[str, Any]],
        id_map: Mapping[str, str],
    ) -> tuple[LifecycleCheckpoint, ...]:
        checkpoints: list[LifecycleCheckpoint] = []
        if events:
            latest = max(str(event["occurred_at"]) for event in events)
            checkpoints.append(
                LifecycleCheckpoint(
                    at=latest,
                    known_as_of=None,
                    expected_visible_event_ids=tuple(str(event["id"]) for event in events),
                    expected_hidden_event_ids=(),
                    expected_status_by_event_id={},
                    worker_action=None,
                )
            )
        for event, message in zip(events, messages, strict=True):
            if superseded := message.get("supersedes_message_id"):
                old_id = id_map.get(f"{message['_session_key']}:{superseded}") or id_map.get(str(superseded))
                if old_id:
                    checkpoints.append(
                        LifecycleCheckpoint(
                            at=str(event["occurred_at"]),
                            known_as_of=None,
                            expected_visible_event_ids=(str(event["id"]),),
                            expected_hidden_event_ids=(old_id,),
                            expected_status_by_event_id={
                                old_id: "superseded",
                                str(event["id"]): "active",
                            },
                            worker_action=None,
                        )
                    )
            if valid_to := self._optional_string(message.get("valid_to")):
                checkpoints.append(
                    LifecycleCheckpoint(
                        at=valid_to,
                        known_as_of=None,
                        expected_visible_event_ids=(),
                        expected_hidden_event_ids=(str(event["id"]),),
                        expected_status_by_event_id={str(event["id"]): "expired"},
                        worker_action="expire_ttl",
                    )
                )
        return tuple(checkpoints)
