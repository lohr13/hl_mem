#!/usr/bin/env python
"""Run the nine paid strict-schema scorer sentinels and persist accounting."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.v0291_behavioral.scorer import (  # noqa: E402
    JUDGE_SCHEMA,
    JUDGE_SYSTEM_PROMPT,
    MODEL_SNAPSHOT,
    BehavioralScorer,
    CompatibleStructuredTransport,
    build_judge_input,
    load_cwd_api_key,
    load_sentinels,
    sentinel_mismatches,
)

INPUT_CNY_PER_MILLION = 2.0
OUTPUT_CNY_PER_MILLION = 8.0
HARD_BUDGET_CNY = 15.0


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _worst_case_reservation(sentinels: list[dict[str, object]], attempts: int) -> float:
    input_bytes = sum(
        len((JUDGE_SYSTEM_PROMPT + _canonical_json(build_judge_input(sentinel, sentinel["trace"]))).encode("utf-8"))
        for sentinel in sentinels
    )
    return (
        input_bytes * attempts * INPUT_CNY_PER_MILLION + len(sentinels) * attempts * 600 * OUTPUT_CNY_PER_MILLION
    ) / 1_000_000


async def _run(output: Path, fixture: Path) -> int:
    sentinels = load_sentinels(fixture)
    max_attempts = 3
    reservation = _worst_case_reservation(sentinels, max_attempts)
    if reservation >= HARD_BUDGET_CNY:
        raise RuntimeError(
            f"sentinel worst-case reservation ¥{reservation:.6f} reaches hard budget ¥{HARD_BUDGET_CNY:.2f}"
        )
    transport = CompatibleStructuredTransport(load_cwd_api_key(), concurrency=8)
    try:
        scorer = BehavioralScorer(transport, max_attempts=max_attempts)
        records = await asyncio.gather(*(scorer.score(sentinel, sentinel["trace"]) for sentinel in sentinels))
    finally:
        await transport.aclose()

    comparisons = [
        {
            "sample_id": sentinel["opaque_sample_id"],
            "mismatches": sentinel_mismatches(record, sentinel),
        }
        for sentinel, record in zip(sentinels, records, strict=True)
    ]
    usage = {
        "input_tokens": sum(record["usage"]["input_tokens"] for record in records),
        "output_tokens": sum(record["usage"]["output_tokens"] for record in records),
    }
    estimated_cost = (
        usage["input_tokens"] * INPUT_CNY_PER_MILLION + usage["output_tokens"] * OUTPUT_CNY_PER_MILLION
    ) / 1_000_000
    passed = all(
        record["call_status"] == "ok" and not comparison["mismatches"]
        for record, comparison in zip(records, comparisons, strict=True)
    )
    artifact = {
        "schema_version": "v0291-judge-smoke-artifact-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": MODEL_SNAPSHOT,
        "fixture_sha256": _sha256_bytes(fixture.read_bytes()),
        "prompt_sha256": _sha256_bytes(JUDGE_SYSTEM_PROMPT.encode("utf-8")),
        "schema_sha256": _sha256_bytes(_canonical_json(JUDGE_SCHEMA).encode("utf-8")),
        "hard_budget_cny": HARD_BUDGET_CNY,
        "worst_case_reserved_cny": reservation,
        "usage": usage,
        "estimated_cost_cny_at_list_price": estimated_cost,
        "passed": passed,
        "valid_count": sum(record["call_status"] == "ok" for record in records),
        "matched_count": sum(not comparison["mismatches"] for comparison in comparisons),
        "comparisons": comparisons,
        "records": records,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(output.resolve()),
                "passed": passed,
                "valid_count": artifact["valid_count"],
                "matched_count": artifact["matched_count"],
                "usage": usage,
                "estimated_cost_cny_at_list_price": estimated_cost,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if passed else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture",
        type=Path,
        default=ROOT / "tests/fixtures/v0291_judge_sentinels.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "evaluation/results/v0291_behavioral_20260820/sentinel_smoke.json",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.output, args.fixture))


if __name__ == "__main__":
    raise SystemExit(main())
