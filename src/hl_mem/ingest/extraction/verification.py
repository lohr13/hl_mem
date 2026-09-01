"""Fail-open entailment verification coordination for extracted Claims."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from hl_mem.ingest.extraction.run_state import ExtractionRunState
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.ingest.verifier import EntailmentVerifier

VerificationMode = Literal["off", "audit", "enforce"]


@dataclass(frozen=True, slots=True)
class VerificationCoordinator:
    verifier: EntailmentVerifier | Any | None
    mode: VerificationMode
    claim_threshold: int
    empty_text_threshold: int
    audit_getter: Callable[[], Any]

    def verify(
        self,
        claims: list[ExtractedClaim],
        source_text: str,
        state: ExtractionRunState,
    ) -> list[ExtractedClaim]:
        """Audit entailment when configured and always preserve the input Claims."""
        if self.verifier is None or self.mode == "off":
            return claims
        if not claims:
            if len(source_text) > self.empty_text_threshold:
                self.audit_getter().emit(
                    "extract",
                    "possible_under_extraction",
                    "observed",
                    detail={
                        "source_length": len(source_text),
                        "length_threshold": self.empty_text_threshold,
                        "verification_mode": self.mode,
                    },
                )
            return claims
        should_verify = self.mode == "enforce" or len(claims) > self.claim_threshold
        if not should_verify:
            return claims
        try:
            results = self.verifier.verify_batch(claims, source_text)
        except Exception as error:
            self.record_usage(state)
            self.emit_failure(error, len(claims))
            return claims
        self.record_usage(state)
        try:
            if len(results) != len(claims):
                raise ValueError("verifier result count does not match claim count")
            for claim_index, (claim, result) in enumerate(zip(claims, results, strict=True)):
                self.audit_getter().emit(
                    "extract",
                    "entailment_checked",
                    result.support_label,
                    detail={
                        "claim_index": claim_index,
                        "claim_subject": claim.subject[:100],
                        "claim_predicate": claim.predicate[:100],
                        "claim_value": claim.value[:100],
                        "rationale": result.rationale[:512],
                        "verification_mode": self.mode,
                    },
                )
        except Exception as error:
            self.emit_failure(error, len(claims))
        return claims

    def emit_failure(self, error: Exception, claim_count: int) -> None:
        self.audit_getter().emit(
            "extract",
            "entailment_verification_failed",
            "error",
            detail={
                "error_class": type(error).__name__,
                "error": str(error).replace("\n", " ")[:256],
                "claim_count": claim_count,
                "verification_mode": self.mode,
            },
        )

    def record_usage(self, state: ExtractionRunState) -> None:
        if self.verifier is None:
            return
        state.total_tokens += int(getattr(self.verifier, "last_usage_tokens", 0))
        state.input_tokens += int(getattr(self.verifier, "last_input_tokens", 0))
        state.output_tokens += int(getattr(self.verifier, "last_output_tokens", 0))
        state.llm_call_count += int(getattr(self.verifier, "last_call_count", 0))


__all__ = ["VerificationCoordinator", "VerificationMode"]
