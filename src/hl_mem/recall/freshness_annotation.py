"""Pure risk-gated freshness annotation policy and aggregate counters."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import Lock
from typing import Literal

from hl_mem.recall.injection import FRESHNESS_POLICY_VERSION, DeliveryPurpose

FreshnessAnnotationMode = Literal["off", "observe", "render"]
RenderKind = Literal["none", "age_only"]

_CURRENT_STATE_SLOT_PREFIXES = ("config.", "state.", "choice.")
_CURRENT_STATE_TAGS = frozenset(
    {"config", "state", "implementation", "dependency", "behavior", "bugfix", "tool_choice"}
)
_STABLE_SLOT_PREFIXES = ("preference.", "identity.")
_STABLE_ATTRIBUTES = frozenset({"memory.explicit"})
_STABLE_TAGS = frozenset({"preference", "identity"})
_ANNOTATION_MARKERS = ("【新鲜度：", "【记录于 ")


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 1) // 2)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _stable_memory(item: "FreshnessItem") -> bool:
    slot = str(item.canonical_slot or "").casefold()
    attribute = str(item.canonical_attribute or "").casefold()
    tags = {tag.casefold() for tag in item.topic_tags}
    return (
        slot.startswith(_STABLE_SLOT_PREFIXES)
        or attribute.startswith(_STABLE_SLOT_PREFIXES)
        or attribute in _STABLE_ATTRIBUTES
        or bool(tags & _STABLE_TAGS)
    )


def _age_display(recorded: datetime, now: datetime) -> tuple[str, str]:
    age_seconds = (now - recorded).total_seconds()
    if age_seconds < 48 * 3600:
        hours = int(age_seconds // 3600)
        bucket = "lt_1h" if hours < 1 else "lt_48h"
        return f"{hours} 小时前", bucket
    days = int(age_seconds // 86_400)
    if days < 365:
        bucket = "lt_30d" if days < 30 else "lt_365d"
        return f"{days} 天前", bucket
    return recorded.date().isoformat(), "gte_365d"


@dataclass(frozen=True, slots=True)
class FreshnessRequest:
    delivery_purpose: DeliveryPurpose
    intent: str
    as_of: str | None
    known_as_of: str | None
    rendering_now: str
    experiment_variant: str = "control"
    policy_version: str = FRESHNESS_POLICY_VERSION


@dataclass(frozen=True, slots=True)
class FreshnessItem:
    item_id: str
    memory_type: str
    text: str
    recorded_from: str | None = None
    canonical_slot: str | None = None
    canonical_attribute: str | None = None
    topic_tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FreshnessDecision:
    item_id: str
    memory_type: str
    eligible: bool
    render_kind: RenderKind
    rendered_text: str
    reason: str
    added_token_estimate: int = 0
    age_bucket: str | None = None
    policy_version: str = FRESHNESS_POLICY_VERSION


@dataclass(frozen=True, slots=True)
class FreshnessEvaluation:
    mode: FreshnessAnnotationMode
    decisions: tuple[FreshnessDecision, ...]
    bypass_reason: str | None = None
    truncation_changed: bool = False

    def with_truncation_changed(self, changed: bool) -> "FreshnessEvaluation":
        return replace(self, truncation_changed=changed)

    def summary(self) -> dict[str, object]:
        eligible = [decision for decision in self.decisions if decision.eligible]
        return {
            "mode": self.mode,
            "policy_version": FRESHNESS_POLICY_VERSION,
            "bypass_reason": self.bypass_reason,
            "eligible": len(eligible),
            "would_render": len(eligible),
            "rendered": len(eligible) if self.mode == "render" else 0,
            "skipped_stable": sum(decision.reason == "skipped_stable" for decision in self.decisions),
            "invalid_time": sum(decision.reason == "invalid_time" for decision in self.decisions),
            "future_time": sum(decision.reason == "future_time" for decision in self.decisions),
            "added_tokens": sum(decision.added_token_estimate for decision in eligible),
            "truncation_changed": self.truncation_changed,
        }


class FreshnessAnnotationPolicy:
    """Classify and render already-loaded claim metadata without storage access."""

    def __init__(self, *, mode: FreshnessAnnotationMode = "off", annotation_token_limit: int = 18) -> None:
        if mode not in {"off", "observe", "render"}:
            raise ValueError("unsupported freshness annotation mode")
        if annotation_token_limit < 1:
            raise ValueError("annotation_token_limit must be positive")
        self.mode = mode
        self.annotation_token_limit = annotation_token_limit

    def evaluate(self, items: list[FreshnessItem], request: FreshnessRequest) -> FreshnessEvaluation:
        bypass_reason = self._bypass_reason(request)
        if bypass_reason is not None:
            return FreshnessEvaluation(self.mode, (), bypass_reason=bypass_reason)
        now = _timestamp(request.rendering_now)
        if now is None:
            return FreshnessEvaluation(self.mode, (), bypass_reason="invalid_rendering_time")
        decisions = tuple(self._evaluate_item(item, request.intent, now) for item in items)
        return FreshnessEvaluation(self.mode, decisions)

    def _evaluate_item(self, item: FreshnessItem, intent: str, now: datetime) -> FreshnessDecision:
        if item.memory_type != "claim":
            return self._skip(item, "non_claim")
        if any(marker in item.text for marker in _ANNOTATION_MARKERS):
            return self._skip(item, "already_annotated")
        if _stable_memory(item):
            return self._skip(item, "skipped_stable")
        if intent in {"tool", "procedure"}:
            allowlisted = True
        elif intent == "current_state":
            slot = str(item.canonical_slot or "").casefold()
            tags = {tag.casefold() for tag in item.topic_tags}
            allowlisted = slot.startswith(_CURRENT_STATE_SLOT_PREFIXES) or bool(tags & _CURRENT_STATE_TAGS)
            if not allowlisted:
                return self._skip(item, "current_state_not_allowlisted")
        else:
            return self._skip(item, "unsupported_intent")
        assert allowlisted
        recorded = _timestamp(item.recorded_from)
        if recorded is None:
            return self._skip(item, "invalid_time")
        if recorded > now:
            return self._skip(item, "future_time")
        display, age_bucket = _age_display(recorded, now)
        label = f"【新鲜度：记录于 {display}；年龄不代表失效，执行前核验当前来源】"
        if _estimate_tokens(label) > self.annotation_token_limit:
            label = f"【记录于 {display}；执行前核验】"
        if _estimate_tokens(label) > self.annotation_token_limit:
            return self._skip(item, "annotation_budget_exceeded")
        rendered = f"{item.text}\n{label}" if item.text else label
        added = _estimate_tokens(rendered) - _estimate_tokens(item.text)
        return FreshnessDecision(
            item_id=item.item_id,
            memory_type=item.memory_type,
            eligible=True,
            render_kind="age_only",
            rendered_text=rendered,
            reason="eligible_age",
            added_token_estimate=added,
            age_bucket=age_bucket,
        )

    @staticmethod
    def _skip(item: FreshnessItem, reason: str) -> FreshnessDecision:
        return FreshnessDecision(item.item_id, item.memory_type, False, "none", item.text, reason)

    def _bypass_reason(self, request: FreshnessRequest) -> str | None:
        if self.mode == "off":
            return "mode_off"
        if request.delivery_purpose != "passive_injection":
            return "non_passive_delivery"
        if request.intent == "historical" or request.as_of or request.known_as_of:
            return "historical_or_bitemporal"
        return None


class FreshnessAnnotationMetrics:
    """Process-local low-cardinality counters for freshness health diagnostics."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._counts = {
            "evaluations": 0,
            "eligible": 0,
            "would_render": 0,
            "rendered": 0,
            "skipped_stable": 0,
            "invalid_time": 0,
            "future_time": 0,
            "added_tokens": 0,
            "truncation_changed": 0,
        }

    def record(self, evaluation: FreshnessEvaluation) -> None:
        increments = {
            "eligible": len([decision for decision in evaluation.decisions if decision.eligible]),
            "would_render": len([decision for decision in evaluation.decisions if decision.eligible]),
            "rendered": (
                len([decision for decision in evaluation.decisions if decision.eligible])
                if evaluation.mode == "render"
                else 0
            ),
            "skipped_stable": sum(decision.reason == "skipped_stable" for decision in evaluation.decisions),
            "invalid_time": sum(decision.reason == "invalid_time" for decision in evaluation.decisions),
            "future_time": sum(decision.reason == "future_time" for decision in evaluation.decisions),
            "added_tokens": sum(
                decision.added_token_estimate for decision in evaluation.decisions if decision.eligible
            ),
            "truncation_changed": int(evaluation.truncation_changed),
        }
        with self._lock:
            self._counts["evaluations"] += 1
            for name, increment in increments.items():
                self._counts[name] += increment

    def snapshot(self, *, mode: FreshnessAnnotationMode) -> dict[str, object]:
        with self._lock:
            return {
                "mode": mode,
                "policy_version": FRESHNESS_POLICY_VERSION,
                "annotation_token_limit": 18,
                "metrics_started_at": self._started_at,
                **self._counts,
            }


DEFAULT_FRESHNESS_ANNOTATION_METRICS = FreshnessAnnotationMetrics()
