"""Pure same-session echo suppression policy and aggregate counters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Literal, Mapping

from hl_mem.recall.injection import ECHO_POLICY_VERSION, DeliveryPurpose

EchoSuppressionMode = Literal["off", "observe", "enforce"]


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


def _age_seconds(now: datetime, value: object) -> float | None:
    parsed = _timestamp(value)
    if parsed is None:
        return None
    age = (now - parsed).total_seconds()
    return age if age >= 0 else None


def _age_bucket(age_seconds: float | None) -> str | None:
    if age_seconds is None:
        return None
    if age_seconds < 600:
        return "lt_10m"
    if age_seconds < 1800:
        return "lt_30m"
    if age_seconds < 3600:
        return "lt_60m"
    if age_seconds < 7200:
        return "lt_2h"
    return "gte_2h"


def _similarity_bucket(value: float | None) -> str | None:
    if value is None:
        return None
    if value >= 0.99:
        return "gte_0_99"
    if value >= 0.97:
        return "gte_0_97"
    if value >= 0.95:
        return "gte_0_95"
    return "lt_0_95"


@dataclass(frozen=True, slots=True)
class EchoRequest:
    delivery_purpose: DeliveryPurpose
    session_id: str | None
    namespace: str
    intent: str
    as_of: str | None
    known_as_of: str | None
    request_now: str
    experiment_variant: str = "control"
    policy_version: str = ECHO_POLICY_VERSION


@dataclass(frozen=True, slots=True)
class EchoDecision:
    claim_id: str
    matched_reason: str | None = None
    would_suppress: bool = False
    suppress: bool = False
    trace_reasons: tuple[str, ...] = ()
    age_bucket: str | None = None
    similarity_bucket: str | None = None


@dataclass(frozen=True, slots=True)
class EchoEvaluation:
    mode: EchoSuppressionMode
    decisions: tuple[EchoDecision, ...]
    bypass_reason: str | None = None
    fail_open_reason: str | None = None
    source_session_resolved: int = 0
    source_session_missing: int = 0

    def summary(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "policy_version": ECHO_POLICY_VERSION,
            "bypass_reason": self.bypass_reason,
            "fail_open_reason": self.fail_open_reason,
            "source_session_resolved": self.source_session_resolved,
            "source_session_missing": self.source_session_missing,
            "would_suppress": sum(item.would_suppress for item in self.decisions),
            "suppressed": sum(item.suppress for item in self.decisions),
            "same_session_recent": sum(item.matched_reason == "same_session_recent" for item in self.decisions),
            "same_session_pending_review": sum(
                item.matched_reason == "same_session_pending_review" for item in self.decisions
            ),
        }


class EchoSuppressionPolicy:
    """Evaluate request-scoped provenance without reading storage or mutating candidates."""

    def __init__(
        self,
        *,
        mode: EchoSuppressionMode = "off",
        session_window_seconds: int = 1800,
        pending_review_enabled: bool = False,
        pending_similarity_threshold: float = 0.95,
        pending_max_seconds: int = 7200,
    ) -> None:
        if mode not in {"off", "observe", "enforce"}:
            raise ValueError("unsupported echo suppression mode")
        if not 60 <= session_window_seconds <= 14_400:
            raise ValueError("session_window_seconds must be between 60 and 14400")
        if not 0.0 <= pending_similarity_threshold <= 1.0:
            raise ValueError("pending_similarity_threshold must be between 0 and 1")
        if pending_max_seconds < 60:
            raise ValueError("pending_max_seconds must be at least 60")
        self.mode = mode
        self.session_window_seconds = session_window_seconds
        self.pending_review_enabled = pending_review_enabled
        self.pending_similarity_threshold = pending_similarity_threshold
        self.pending_max_seconds = pending_max_seconds

    def evaluate(
        self,
        claim_ids: list[str],
        request: EchoRequest,
        signals: Mapping[str, Mapping[str, object]],
    ) -> EchoEvaluation:
        bypass_reason = self._bypass_reason(request)
        if bypass_reason is not None:
            return EchoEvaluation(
                mode=self.mode,
                decisions=tuple(EchoDecision(claim_id) for claim_id in claim_ids),
                bypass_reason=bypass_reason,
            )
        if not request.session_id:
            return EchoEvaluation(
                mode=self.mode,
                decisions=tuple(EchoDecision(claim_id) for claim_id in claim_ids),
                fail_open_reason="missing_request_session",
            )
        now = _timestamp(request.request_now)
        if now is None:
            return EchoEvaluation(
                mode=self.mode,
                decisions=tuple(
                    EchoDecision(claim_id, trace_reasons=("echo_suppression_fail_open",)) for claim_id in claim_ids
                ),
                fail_open_reason="invalid_request_time",
                source_session_missing=len(claim_ids),
            )

        decisions: list[EchoDecision] = []
        resolved = 0
        missing = 0
        for claim_id in claim_ids:
            signal = signals.get(claim_id)
            if signal is None or signal.get("source_session_resolved") is not True:
                missing += 1
                decisions.append(EchoDecision(claim_id, trace_reasons=("echo_suppression_fail_open",)))
                continue
            resolved += 1
            session_age = _age_seconds(now, signal.get("matching_session_recorded_at"))
            reason: str | None = None
            matched_age: float | None = None
            similarity: float | None = None
            if session_age is not None and session_age < self.session_window_seconds:
                reason = "same_session_recent"
                matched_age = session_age
            elif self.pending_review_enabled:
                raw_similarity = signal.get("pending_similarity")
                similarity = float(raw_similarity) if isinstance(raw_similarity, (int, float)) else None
                pending_age = _age_seconds(now, signal.get("pending_created_at"))
                if (
                    session_age is not None
                    and similarity is not None
                    and similarity >= self.pending_similarity_threshold
                    and pending_age is not None
                    and pending_age < self.pending_max_seconds
                ):
                    reason = "same_session_pending_review"
                    matched_age = pending_age
            if reason is None:
                decisions.append(EchoDecision(claim_id))
                continue
            observe = self.mode == "observe"
            decisions.append(
                EchoDecision(
                    claim_id=claim_id,
                    matched_reason=reason,
                    would_suppress=True,
                    suppress=self.mode == "enforce",
                    trace_reasons=(reason, "echo_suppression_observe_only") if observe else (reason,),
                    age_bucket=_age_bucket(matched_age),
                    similarity_bucket=_similarity_bucket(similarity),
                )
            )
        return EchoEvaluation(
            mode=self.mode,
            decisions=tuple(decisions),
            source_session_resolved=resolved,
            source_session_missing=missing,
        )

    def read_failure(self, claim_ids: list[str], reason: str = "signal_read_error") -> EchoEvaluation:
        return EchoEvaluation(
            mode=self.mode,
            decisions=tuple(
                EchoDecision(claim_id, trace_reasons=("echo_suppression_fail_open",)) for claim_id in claim_ids
            ),
            fail_open_reason=reason,
            source_session_missing=len(claim_ids),
        )

    def _bypass_reason(self, request: EchoRequest) -> str | None:
        if self.mode == "off":
            return "mode_off"
        if request.delivery_purpose != "passive_injection":
            return "non_passive_delivery"
        if request.intent == "historical" or request.as_of or request.known_as_of:
            return "historical_or_bitemporal"
        return None


class EchoSuppressionMetrics:
    """Process-local aggregate counters exposed by health without storing content."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._counts = {
            "evaluations": 0,
            "source_session_resolved": 0,
            "source_session_missing": 0,
            "would_suppress": 0,
            "suppressed": 0,
            "same_session_recent": 0,
            "same_session_pending_review": 0,
            "fail_open": 0,
        }

    def record(self, evaluation: EchoEvaluation) -> None:
        increments = {
            "source_session_resolved": evaluation.source_session_resolved,
            "source_session_missing": evaluation.source_session_missing,
            "would_suppress": sum(item.would_suppress for item in evaluation.decisions),
            "suppressed": sum(item.suppress for item in evaluation.decisions),
            "same_session_recent": sum(item.matched_reason == "same_session_recent" for item in evaluation.decisions),
            "same_session_pending_review": sum(
                item.matched_reason == "same_session_pending_review" for item in evaluation.decisions
            ),
        }
        with self._lock:
            self._counts["evaluations"] += 1
            for name, increment in increments.items():
                self._counts[name] += increment
            self._counts["fail_open"] += int(evaluation.fail_open_reason is not None)

    def snapshot(
        self,
        *,
        mode: EchoSuppressionMode,
        session_window_seconds: int,
        pending_review_enabled: bool,
    ) -> dict[str, object]:
        with self._lock:
            return {
                "mode": mode,
                "policy_version": ECHO_POLICY_VERSION,
                "session_window_seconds": session_window_seconds,
                "pending_review_enabled": pending_review_enabled,
                "metrics_started_at": self._started_at,
                **self._counts,
            }


DEFAULT_ECHO_SUPPRESSION_METRICS = EchoSuppressionMetrics()
