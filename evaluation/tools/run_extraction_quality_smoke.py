"""Run the fixed extraction-quality smoke corpus against one real LLM provider."""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hl_mem.components import make_extractor
from hl_mem.config.loader import load_settings
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.ingest.llm_extractor import PROMPT_HASH
from hl_mem.observability.audit import audit_scope


@dataclass(frozen=True)
class ExpectedClaim:
    subject: str
    term_groups: tuple[tuple[str, ...], ...]
    predicate: str | None = None


@dataclass(frozen=True)
class SmokeCase:
    case_id: str
    occurred_at: str
    messages: tuple[dict[str, str], ...]
    required_claims: tuple[ExpectedClaim, ...]
    forbidden_subjects: frozenset[str]
    expect_empty: bool


@dataclass(frozen=True)
class SmokeScore:
    passed: bool
    covered_targets: int
    target_count: int
    missing_targets: tuple[int, ...]
    forbidden_subject_hits: tuple[str, ...]


class _RecordingAudit:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str, dict[str, Any]]] = []

    def emit(self, phase: str, action: str, outcome: str, *, detail: Any = None, **_: Any) -> bool:
        self.events.append((phase, action, outcome, dict(detail or {})))
        return True


def _matches(claim: ExtractedClaim, expected: ExpectedClaim) -> bool:
    searchable = f"{claim.subject} {claim.value}"
    return (
        claim.subject == expected.subject
        and (expected.predicate is None or claim.predicate == expected.predicate)
        and all(any(term in searchable for term in group) for group in expected.term_groups)
    )


def score_case(case: SmokeCase, claims: Sequence[ExtractedClaim]) -> SmokeScore:
    missing = tuple(
        index
        for index, expected in enumerate(case.required_claims)
        if not any(_matches(claim, expected) for claim in claims)
    )
    forbidden = tuple(sorted({claim.subject for claim in claims if claim.subject in case.forbidden_subjects}))
    empty_ok = not claims if case.expect_empty else True
    return SmokeScore(
        passed=empty_ok and not missing and not forbidden,
        covered_targets=len(case.required_claims) - len(missing),
        target_count=len(case.required_claims),
        missing_targets=missing,
        forbidden_subject_hits=forbidden,
    )


def load_cases(path: Path) -> tuple[SmokeCase, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("schema_version must be 1")
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list):
        raise ValueError("cases must be a list")

    case_ids: set[str] = set()
    cases: list[SmokeCase] = []
    for raw_case in raw_cases:
        if not isinstance(raw_case, dict):
            raise ValueError("case must be an object")
        case_id = str(raw_case.get("id", ""))
        if not case_id:
            raise ValueError("case id is required")
        if case_id in case_ids:
            raise ValueError(f"duplicate case id: {case_id}")
        case_ids.add(case_id)
        raw_required = raw_case.get("required_claims", [])
        if not isinstance(raw_required, list):
            raise ValueError("required_claims must be a list")
        required_claims: list[ExpectedClaim] = []
        for raw_expected in raw_required:
            if not isinstance(raw_expected, dict):
                raise ValueError("required claim must be an object")
            raw_groups = raw_expected.get("term_groups", [])
            if not isinstance(raw_groups, list):
                raise ValueError("term_groups must be a list")
            groups: list[tuple[str, ...]] = []
            for raw_group in raw_groups:
                if not isinstance(raw_group, list) or not raw_group:
                    raise ValueError("empty term group")
                groups.append(tuple(str(term) for term in raw_group))
            required_claims.append(
                ExpectedClaim(
                    subject=str(raw_expected.get("subject", "")),
                    predicate=None if raw_expected.get("predicate") is None else str(raw_expected["predicate"]),
                    term_groups=tuple(groups),
                )
            )
        expect_empty = bool(raw_case.get("expect_empty", False))
        if expect_empty and required_claims:
            raise ValueError("empty case cannot require claims")
        raw_messages = raw_case.get("messages", [])
        if not isinstance(raw_messages, list) or not all(isinstance(message, dict) for message in raw_messages):
            raise ValueError("messages must be a list of objects")
        cases.append(
            SmokeCase(
                case_id=case_id,
                occurred_at=str(raw_case.get("occurred_at", "")),
                messages=tuple({str(key): str(value) for key, value in message.items()} for message in raw_messages),
                required_claims=tuple(required_claims),
                forbidden_subjects=frozenset(str(subject) for subject in raw_case.get("forbidden_subjects", [])),
                expect_empty=expect_empty,
            )
        )
    return tuple(cases)


def _source_for_case(case: SmokeCase) -> tuple[dict[str, Any], dict[str, Any]]:
    source_events = [
        {
            "id": f"extraction-quality-smoke:{case.case_id}:{index}",
            "actor_type": message["speaker"],
            "content": {"text": message["text"]},
            "occurred_at": case.occurred_at,
        }
        for index, message in enumerate(case.messages)
    ]
    content = {
        "messages": [
            {
                "event_index": index,
                "speaker": message["speaker"],
                "turn": index,
                "occurred_at": case.occurred_at,
                "content": message["text"],
            }
            for index, message in enumerate(case.messages)
        ]
    }
    context = {
        "occurred_at": case.occurred_at,
        "actor_type": "conversation",
        "event_type": "message",
        "session_id": None,
        "recent_events": [],
        "_source_events": source_events,
    }
    return content, context


def _budget_counts(audit: _RecordingAudit, claims: Sequence[ExtractedClaim]) -> tuple[int, int]:
    for phase, action, outcome, detail in audit.events:
        if (phase, action, outcome) == ("extract", "claim_budget", "overflow_truncated"):
            return int(detail["generated_claim_count"]), int(detail["retained_claim_count"])
    return len(claims), len(claims)


def _claim_summaries(claims: Sequence[ExtractedClaim]) -> list[dict[str, str]]:
    return [{"subject": claim.subject, "predicate": claim.predicate, "value": claim.value} for claim in claims]


def run(args: argparse.Namespace) -> int:
    cases = load_cases(args.fixture)
    settings = replace(
        load_settings(args.config, args.env_file),
        verification_mode="off",
        llm_schema_retries=0,
        llm_max_attempts=1,
    )
    extractor = make_extractor(settings, require_real=True)
    results: list[dict[str, Any]] = []
    try:
        for case in cases:
            content, context = _source_for_case(case)
            audit = _RecordingAudit()
            started = time.monotonic()
            with audit_scope(audit, event_id=f"extraction-quality-smoke:{case.case_id}"):
                claims = extractor.extract(content, context)
            latency_ms = round((time.monotonic() - started) * 1000, 3)
            score = score_case(case, claims)
            generated_count, retained_count = _budget_counts(audit, claims)
            results.append(
                {
                    "id": case.case_id,
                    "passed": score.passed,
                    "expect_empty": case.expect_empty,
                    "covered_targets": score.covered_targets,
                    "target_count": score.target_count,
                    "missing_targets": list(score.missing_targets),
                    "forbidden_subject_hits": list(score.forbidden_subject_hits),
                    "generated_count": generated_count,
                    "retained_count": retained_count,
                    "llm_calls": int(getattr(extractor, "last_llm_call_count", 0)),
                    "input_tokens": int(getattr(extractor, "last_input_tokens", 0)),
                    "output_tokens": int(getattr(extractor, "last_output_tokens", 0)),
                    "latency_ms": latency_ms,
                    "claim_summaries": _claim_summaries(claims),
                }
            )
    finally:
        close = getattr(getattr(extractor, "llm_client", None), "close", None)
        if callable(close):
            close()

    report = {
        "schema_version": "extraction-quality-smoke-v1",
        "label": args.label,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": {
            "provider": settings.llm_provider,
            "model": settings.llm_model,
            "prompt_hash": PROMPT_HASH,
        },
        "summary": {
            "passed": all(item["passed"] for item in results),
            "cases": len(results),
            "passed_cases": sum(bool(item["passed"]) for item in results),
            "target_coverage": sum(item["covered_targets"] for item in results)
            / max(1, sum(item["target_count"] for item in results)),
            "negative_violations": sum(bool(item["expect_empty"] and item["retained_count"]) for item in results),
            "llm_calls": sum(item["llm_calls"] for item in results),
            "input_tokens": sum(item["input_tokens"] for item in results),
            "output_tokens": sum(item["output_tokens"] for item in results),
        },
        "cases": results,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return (
        0 if all(item["passed"] and item["llm_calls"] == 1 and item["retained_count"] <= 16 for item in results) else 1
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path("tests/eval/fixtures/extraction_quality_smoke_v1.json"),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
