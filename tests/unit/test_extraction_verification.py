from __future__ import annotations

from hl_mem.ingest.extraction.run_state import ExtractionRunState
from hl_mem.ingest.extraction.verification import VerificationCoordinator
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.ingest.verifier import EntailmentResult

CLAIMS = [ExtractedClaim(predicate="uses", value="SQLite", subject="HL-Mem")]


class _Audit:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str, dict[str, object]]] = []

    def emit(self, phase: str, action: str, outcome: str, *, detail=None, **_dimensions) -> bool:
        self.events.append((phase, action, outcome, detail or {}))
        return True


class _Verifier:
    last_usage_tokens = 17
    last_input_tokens = 12
    last_output_tokens = 5
    last_call_count = 1

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    def verify_batch(self, claims: list[ExtractedClaim], source_text: str) -> list[EntailmentResult]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return [EntailmentResult("entailed", f"supported by {source_text}") for _claim in claims]


def _coordinator(verifier: _Verifier, audit: _Audit, *, mode: str = "enforce") -> VerificationCoordinator:
    return VerificationCoordinator(
        verifier=verifier,
        mode=mode,
        claim_threshold=5,
        empty_text_threshold=10,
        audit_getter=lambda: audit,
    )


def test_verifier_usage_merges_into_shared_run_state() -> None:
    state = ExtractionRunState()
    audit = _Audit()

    result = _coordinator(_Verifier(), audit).verify(CLAIMS, "source", state)

    assert result == CLAIMS
    assert (state.input_tokens, state.output_tokens, state.total_tokens, state.llm_call_count) == (12, 5, 17, 1)
    assert [(action, outcome) for _phase, action, outcome, _detail in audit.events] == [
        ("entailment_checked", "entailed")
    ]


def test_audit_mode_below_threshold_does_not_call_verifier() -> None:
    verifier = _Verifier()
    state = ExtractionRunState()

    assert _coordinator(verifier, _Audit(), mode="audit").verify(CLAIMS, "source", state) == CLAIMS
    assert verifier.calls == 0
    assert state.llm_call_count == 0


def test_verifier_failure_is_redacted_and_fail_open() -> None:
    verifier = _Verifier(error=RuntimeError("private\n" + "x" * 400))
    state = ExtractionRunState()
    audit = _Audit()

    assert _coordinator(verifier, audit).verify(CLAIMS, "source", state) == CLAIMS

    [_phase, action, outcome, detail] = audit.events[0]
    assert (action, outcome) == ("entailment_verification_failed", "error")
    assert detail["error_class"] == "RuntimeError"
    assert "\n" not in str(detail["error"])
    assert len(str(detail["error"])) == 256
    assert state.llm_call_count == 1


def test_long_empty_source_emits_under_extraction_without_verifier_call() -> None:
    verifier = _Verifier()
    state = ExtractionRunState()
    audit = _Audit()

    assert _coordinator(verifier, audit, mode="audit").verify([], "source longer than threshold", state) == []
    assert verifier.calls == 0
    assert [(action, outcome) for _phase, action, outcome, _detail in audit.events] == [
        ("possible_under_extraction", "observed")
    ]
