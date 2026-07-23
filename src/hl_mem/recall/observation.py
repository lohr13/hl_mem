"""基于多条 Claim 证据构建 Observation 派生记忆。"""

from __future__ import annotations

import json
from typing import Any


class ObservationBuilder:
    MIN_PROOFS = 2
    MIN_SOURCES = 1

    def try_build(self, claims: list[dict[str, Any]]) -> dict[str, Any] | None:
        active = [claim for claim in claims if claim.get("status", "active") == "active"]
        if len(active) < self.MIN_PROOFS or not self._same_topic(active):
            return None
        events = sorted({event for claim in active for event in self._event_ids(claim)})
        if len(events) < self.MIN_PROOFS:
            return None
        dates = sorted(filter(None, (claim.get("observed_at") for claim in active)))
        summary = "；".join(str(self._value(claim)) for claim in active)
        earliest = dates[0] if dates else "未知"
        latest = dates[-1] if dates else "未知"
        return {
            "body": f"基于 {len(active)} 条证据：{summary}\n来源：{','.join(events)}\n"
                    f"最早观察：{earliest}，最近观察：{latest}",
            "claim_ids": [claim["id"] for claim in active], "event_ids": events,
            "confidence": sum(float(claim.get("confidence", .5)) for claim in active) / len(active),
        }

    @staticmethod
    def _same_topic(claims: list[dict[str, Any]]) -> bool:
        keys = {claim.get("conflict_key") for claim in claims if claim.get("conflict_key")}
        topics = {(claim.get("subject_entity_id"), claim.get("predicate")) for claim in claims}
        return len(keys) == 1 or len(topics) == 1

    @staticmethod
    def _event_ids(claim: dict[str, Any]) -> list[str]:
        values = claim.get("event_ids") or claim.get("evidence") or []
        return [item.get("evidence_id", item.get("event_id", item.get("id")))
                if isinstance(item, dict) else item for item in values]

    @staticmethod
    def _value(claim: dict[str, Any]) -> Any:
        value = claim.get("value", claim.get("value_json", ""))
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value
