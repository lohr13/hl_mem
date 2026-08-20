#!/usr/bin/env python
"""Run the frozen v0.29.1 structural and behavioral evaluation phases."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.v0291_behavioral.manifest import (  # noqa: E402
    load_behavioral_manifest,
)
from evaluation.v0291_behavioral.runner import (  # noqa: E402
    BudgetedTransport,
    BudgetExceeded,
    BudgetLedger,
    GateBlocked,
    build_frozen_run_manifest,
    require_sentinel_gate,
    run_behavioral_phase,
    run_sentinel_phase,
    run_structural_phase,
)
from evaluation.v0291_behavioral.scorer import (  # noqa: E402
    CompatibleStructuredTransport,
    load_cwd_api_key,
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


async def _run_paid(
    *,
    phase: str,
    output_dir: Path,
    behavior_manifest_path: Path,
    sentinel_fixture_path: Path,
    sentinel_artifact_path: Path,
    budget_cny: float,
) -> tuple[int, dict[str, object]]:
    ledger = BudgetLedger(hard_budget_cny=budget_cny)
    base_transport = CompatibleStructuredTransport(
        load_cwd_api_key(),
        concurrency=8,
    )
    transport = BudgetedTransport(base_transport, ledger)
    try:
        if phase in {"sentinel", "all"}:
            sentinel = await run_sentinel_phase(
                transport,
                sentinel_fixture_path,
                sentinel_artifact_path,
            )
            if not sentinel["passed"]:
                return 2, ledger.snapshot()
        if phase in {"behavioral", "all"}:
            await run_behavioral_phase(
                transport=transport,
                behavior_manifest_path=behavior_manifest_path,
                sentinel_artifact_path=sentinel_artifact_path,
                output_dir=output_dir,
            )
        return 0, ledger.snapshot()
    finally:
        await base_transport.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase",
        choices=("structural", "sentinel", "behavioral", "all"),
        default="all",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "evaluation/results/v0291_behavioral_20260820",
    )
    parser.add_argument(
        "--structural-fixture",
        type=Path,
        default=ROOT / "tests/fixtures/v0291_injection_replay.json",
    )
    parser.add_argument(
        "--behavior-manifest",
        type=Path,
        default=ROOT / "tests/fixtures/v0291_freshness_behavioral.json",
    )
    parser.add_argument(
        "--sentinel-fixture",
        type=Path,
        default=ROOT / "tests/fixtures/v0291_judge_sentinels.json",
    )
    parser.add_argument("--sentinel-artifact", type=Path)
    parser.add_argument("--budget-cny", type=float, default=15.0)
    args = parser.parse_args(argv)
    output_dir = args.output_dir.resolve()
    sentinel_artifact = (
        args.sentinel_artifact.resolve() if args.sentinel_artifact is not None else output_dir / "sentinel_smoke.json"
    )
    behavior_manifest = load_behavioral_manifest(args.behavior_manifest)
    frozen = build_frozen_run_manifest(
        structure_fixture_path=args.structural_fixture,
        behavior_manifest_path=args.behavior_manifest,
        sentinel_path=args.sentinel_fixture,
        behavior_manifest=behavior_manifest,
    )
    _write_json(output_dir / "run_manifest.json", frozen)

    try:
        if args.phase in {"structural", "all"}:
            structural = run_structural_phase(args.structural_fixture, output_dir)
            if structural["gates"]["structural_passed"] is not True:
                raise GateBlocked("structural replay gate failed")
        if args.phase == "structural":
            print(
                json.dumps(
                    {
                        "phase": "structural",
                        "output_dir": str(output_dir),
                        "structural_passed": True,
                        "llm_calls": 0,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.phase == "behavioral":
            require_sentinel_gate(sentinel_artifact)
        result, budget = asyncio.run(
            _run_paid(
                phase=args.phase,
                output_dir=output_dir,
                behavior_manifest_path=args.behavior_manifest,
                sentinel_fixture_path=args.sentinel_fixture,
                sentinel_artifact_path=sentinel_artifact,
                budget_cny=args.budget_cny,
            )
        )
        _write_json(output_dir / "budget_summary.json", budget)
        print(
            json.dumps(
                {
                    "phase": args.phase,
                    "output_dir": str(output_dir),
                    "exit_code": result,
                    "budget": budget,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return result
    except GateBlocked as error:
        print(
            json.dumps(
                {
                    "phase": args.phase,
                    "output_dir": str(output_dir),
                    "blocked": str(error),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    except BudgetExceeded as error:
        print(
            json.dumps(
                {
                    "phase": args.phase,
                    "output_dir": str(output_dir),
                    "budget_exceeded": str(error),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
