from __future__ import annotations

from unittest.mock import Mock

import pytest

from hl_mem.domain.recall import RecallIntent
from hl_mem.recall.echo_suppression import EchoRequest, EchoSuppressionPolicy
from hl_mem.recall.staged_pipeline import RecallContext, _apply_echo_suppression, _rerank
from hl_mem.recall.trace import SearchPhaseMetrics, SearchTrace, SearchTracer
from hl_mem.storage.claims import ClaimRepository

NOW = "2026-08-18T12:00:00+00:00"


def _request(**changes: object) -> EchoRequest:
    values = {
        "delivery_purpose": "passive_injection",
        "session_id": "session-1",
        "namespace": "default",
        "intent": "current_state",
        "as_of": None,
        "known_as_of": None,
        "request_now": NOW,
    }
    values.update(changes)
    return EchoRequest(**values)  # type: ignore[arg-type]


def _signals(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "source_session_resolved": True,
        "matching_session_recorded_at": "2026-08-18T11:45:00+00:00",
        "pending_similarity": None,
        "pending_created_at": None,
    }
    values.update(changes)
    return values


def test_echo_observe_marks_recent_same_session_without_changing_result() -> None:
    """Catches observe accidentally filtering a candidate or failing to emit the stable reasons."""
    policy = EchoSuppressionPolicy(mode="observe", session_window_seconds=1800)

    evaluation = policy.evaluate(["claim-1"], _request(), {"claim-1": _signals()})
    decision = evaluation.decisions[0]

    assert decision.matched_reason == "same_session_recent"
    assert decision.would_suppress is True
    assert decision.suppress is False
    assert decision.trace_reasons == ("same_session_recent", "echo_suppression_observe_only")
    assert decision.age_bucket == "lt_30m"


def test_echo_enforce_filters_only_matching_recent_or_bounded_pending_claims() -> None:
    """Catches global age filtering, stale pending filtering, or suppression of the old pair endpoint."""
    policy = EchoSuppressionPolicy(
        mode="enforce",
        session_window_seconds=1800,
        pending_review_enabled=True,
        pending_similarity_threshold=0.95,
        pending_max_seconds=7200,
    )
    signals = {
        "recent": _signals(),
        "pending": _signals(
            matching_session_recorded_at="2026-08-18T10:00:00+00:00",
            pending_similarity=0.96,
            pending_created_at="2026-08-18T11:00:00+00:00",
        ),
        "old-pending": _signals(
            matching_session_recorded_at="2026-08-18T09:00:00+00:00",
            pending_similarity=0.99,
            pending_created_at="2026-08-18T09:00:00+00:00",
        ),
        "cross-session": _signals(
            matching_session_recorded_at=None,
            pending_similarity=0.99,
            pending_created_at="2026-08-18T11:00:00+00:00",
        ),
    }

    evaluation = policy.evaluate(list(signals), _request(), signals)
    suppressed = {decision.claim_id: decision.matched_reason for decision in evaluation.decisions if decision.suppress}

    assert suppressed == {
        "recent": "same_session_recent",
        "pending": "same_session_pending_review",
    }


@pytest.mark.parametrize(
    "request_context",
    (
        _request(delivery_purpose="active_recall"),
        _request(delivery_purpose="api"),
        _request(intent="historical"),
        _request(as_of="2026-01-01T00:00:00+00:00"),
        _request(known_as_of="2026-01-01T00:00:00+00:00"),
    ),
)
def test_echo_explicit_and_historical_paths_always_bypass(request_context: EchoRequest) -> None:
    """Catches passive injection governance leaking into explicit or bitemporal recall."""
    evaluation = EchoSuppressionPolicy(mode="enforce").evaluate(
        ["claim-1"],
        request_context,
        {"claim-1": _signals()},
    )

    assert evaluation.decisions[0].suppress is False
    assert evaluation.bypass_reason is not None


def test_echo_missing_request_or_source_session_fails_open() -> None:
    """Catches missing provenance falling back to global claim-age suppression."""
    policy = EchoSuppressionPolicy(mode="enforce")

    missing_request = policy.evaluate(["claim-1"], _request(session_id=None), {"claim-1": _signals()})
    missing_source = policy.evaluate(
        ["claim-1"],
        _request(),
        {"claim-1": _signals(source_session_resolved=False, matching_session_recorded_at=None)},
    )

    assert missing_request.decisions[0].suppress is False
    assert missing_request.fail_open_reason == "missing_request_session"
    assert missing_source.decisions[0].suppress is False
    assert missing_source.decisions[0].trace_reasons == ("echo_suppression_fail_open",)
    assert missing_source.source_session_missing == 1


class _RecordingReranker:
    def __init__(self) -> None:
        self.documents: list[str] = []

    def rerank(self, _query: str, documents: list[str], top_n: int = 20) -> list[tuple[int, float]]:
        self.documents = list(documents)
        return [(index, 1.0 - index / 10) for index in range(min(top_n, len(documents)))]


def _pipeline_context(mode: str) -> tuple[RecallContext, _RecordingReranker, SearchTracer]:
    claims = [
        {"id": "echo", "index_text": "echo text", "recorded_from": NOW},
        {"id": "useful", "index_text": "useful text", "recorded_from": NOW},
        {"id": "useful-2", "index_text": "other useful text", "recorded_from": NOW},
    ]
    features = {
        claim["id"]: {
            "semantic": 0.5,
            "recency": 0.0,
            "access_frequency": 0.0,
            "confidence": 0.0,
            "importance": 0.0,
            "utility": 0.0,
        }
        for claim in claims
    }
    tracer = SearchTracer(SearchTrace("query-1", "hash", "current_state", 2, 10, {}, SearchPhaseMetrics()))
    reranker = _RecordingReranker()
    context = RecallContext(
        repo=Mock(spec=ClaimRepository),
        query="query",
        reranker=reranker,
        candidate_limit=10,
        selected_intent=RecallIntent.CURRENT_STATE,
        feature_by_id=features,
        pre_scores={"echo": 0.8, "useful": 0.7},
        ranked_claims=claims,
        tracer=tracer,
        echo_policy=EchoSuppressionPolicy(mode=mode),
        echo_request=_request(),
        echo_signal_loader=lambda _ids: {
            "echo": _signals(),
            "useful": _signals(matching_session_recorded_at=None),
            "useful-2": _signals(matching_session_recorded_at=None),
        },
    )
    return context, reranker, tracer


def test_echo_stage_runs_after_expansion_and_before_reranker() -> None:
    """Catches the reranker receiving enforce-filtered echo text or observe changing its input."""
    observe, observe_reranker, observe_trace = _pipeline_context("observe")
    enforce, enforce_reranker, enforce_trace = _pipeline_context("enforce")

    _rerank(_apply_echo_suppression(observe))
    _rerank(_apply_echo_suppression(enforce))

    assert observe_reranker.documents == ["echo text", "useful text", "other useful text"]
    assert enforce_reranker.documents == ["useful text", "other useful text"]
    assert observe_trace.to_dict()["candidates"]["echo"]["filter_reasons"] == [
        "same_session_recent",
        "echo_suppression_observe_only",
    ]
    assert enforce_trace.to_dict()["candidates"]["echo"]["filter_reasons"] == ["same_session_recent"]
