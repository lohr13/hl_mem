"""Run and score the frozen GLM-5.3-flash reasoning_effort=low arm D."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence

import httpx

EQUIPMENT_DIR = Path(__file__).resolve().parent
BASE_RUNNER_PATH = EQUIPMENT_DIR / "run_prompt_density_ab.py"
BASE_SPEC = importlib.util.spec_from_file_location("prompt_density_ab_base", BASE_RUNNER_PATH)
if BASE_SPEC is None or BASE_SPEC.loader is None:
    raise RuntimeError(f"cannot load frozen runner: {BASE_RUNNER_PATH}")
base = importlib.util.module_from_spec(BASE_SPEC)
BASE_SPEC.loader.exec_module(base)

PROTOCOL_ID = "prompt_density_glm_effort_low_20260829_v1"
ARM = "D"
SOURCE_PROMPT_ARM = "B"
MODEL = "glm-5.3-flash"
BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
ENDPOINT = f"{BASE_URL}/chat/completions"
THINKING = {"type": "enabled"}
REASONING_EFFORT = "low"
CONCURRENCY = 4
TIMEOUT_SECONDS = 120.0
MAX_ATTEMPTS = 1
INPUT_PRICE_PER_MILLION = 0.4
OUTPUT_PRICE_PER_MILLION = 1.4

DEFAULT_MANIFEST = EQUIPMENT_DIR / "manifest.json"
DEFAULT_ENV_FILE = EQUIPMENT_DIR.parent / "softsplit_ab_20260827/.env_flash"
DEFAULT_GOLD_SOURCE = EQUIPMENT_DIR / "manual_review_c1024.json"
DEFAULT_C_REPORT = EQUIPMENT_DIR / "report_c1024.json"
DEFAULT_RUNS = EQUIPMENT_DIR / "runs_glm_effort_low.jsonl"
DEFAULT_METADATA = EQUIPMENT_DIR / "run_metadata_glm_effort_low.json"
DEFAULT_REVIEW = EQUIPMENT_DIR / "manual_review_glm_effort_low.json"
DEFAULT_REPORT = EQUIPMENT_DIR / "report_glm_effort_low.json"
DEFAULT_GATE_TABLE = EQUIPMENT_DIR / "gate_table_glm_effort_low.md"
DEFAULT_COMPARISON = EQUIPMENT_DIR / "comparison_d_c1024.csv"


def build_payload(messages: Sequence[Mapping[str, str]], *, language: str) -> dict[str, Any]:
    full_messages = [{"role": "system", "content": base.system_prompt(language, SOURCE_PROMPT_ARM)}]
    full_messages.extend(
        {"role": str(message["role"]), "content": str(message["content"])}
        for message in messages
    )
    return {
        "model": MODEL,
        "messages": full_messages,
        "thinking": dict(THINKING),
        "reasoning_effort": REASONING_EFFORT,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "extraction_response",
                "schema": base.response_schema(SOURCE_PROMPT_ARM),
                "strict": True,
            },
        },
    }


def usage_cost_cny(usage: Mapping[str, Any]) -> float:
    return (
        int(usage.get("input_tokens") or 0) * INPUT_PRICE_PER_MILLION
        + int(usage.get("output_tokens") or 0) * OUTPUT_PRICE_PER_MILLION
    ) / 1_000_000


def _runtime_for_dense_case(manifest: Mapping[str, Any], case: Mapping[str, Any]) -> dict[str, Any]:
    if case.get("case_type") != "dense":
        raise ValueError(f"arm D accepts dense cases only: {case.get('case_id')}")
    source = manifest.get("source")
    if not isinstance(source, Mapping):
        raise ValueError("manifest source is missing")
    runtime = base.load_dense_runtime(Path(str(source["dense_database"])), case)
    if runtime["body_sha256"] != case.get("body_sha256"):
        raise ValueError(f"prepared body hash changed: {case.get('case_id')}")
    return runtime


def _base_record(case: Mapping[str, Any], runtime: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "protocol_id": PROTOCOL_ID,
        "completed_at": base.utc_now(),
        "case_id": str(case["case_id"]),
        "case_type": "dense",
        "arm": ARM,
        "pair_order": [ARM],
        "language": str(runtime["language"]),
        "body_length": int(runtime["body_length"]),
        "body_sha256": str(runtime["body_sha256"]),
        "request_fingerprint": base.sha256_text(base.canonical_json(payload)),
        "request_started": False,
        "attempt_count": 0,
        "configuration": {
            "provider": "zhipu",
            "model": MODEL,
            "base_url": BASE_URL,
            "thinking": dict(THINKING),
            "reasoning_effort": REASONING_EFFORT,
            "strict_json_schema": True,
            "max_tokens": None,
            "enable_thinking": None,
            "timeout_seconds": TIMEOUT_SECONDS,
            "max_attempts": MAX_ATTEMPTS,
            "schema_max_items": 30,
            "prompt_sha256": base.sha256_text(base.system_prompt(str(runtime["language"]), SOURCE_PROMPT_ARM)),
            "schema_sha256": base.sha256_text(base.canonical_json(base.response_schema(SOURCE_PROMPT_ARM))),
            "prompt_variant": "B_density_max30",
        },
    }


def _execute_case(
    client: httpx.Client,
    api_key: str,
    case: Mapping[str, Any],
    runtime: Mapping[str, Any],
) -> dict[str, Any]:
    payload = build_payload(runtime["messages"], language=str(runtime["language"]))
    record = _base_record(case, runtime, payload)
    started = time.perf_counter()
    request_dispatched = False
    try:
        request_dispatched = True
        response = client.post(
            ENDPOINT,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=httpx.Timeout(TIMEOUT_SECONDS),
        )
        response.raise_for_status()
        envelope = response.json()
        if not isinstance(envelope, dict):
            raise TypeError("provider response body is not an object")
        usage = base._response_usage(envelope)
        content = base._assistant_content(envelope)
        schema_valid, parsed, schema_errors = base._validate_assistant(content, SOURCE_PROMPT_ARM)
        claims = parsed.get("claims", []) if isinstance(parsed, dict) and isinstance(parsed.get("claims"), list) else []
        choices = envelope.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], Mapping) else {}
        return {
            **record,
            "completed_at": base.utc_now(),
            "request_started": True,
            "attempt_count": 1,
            "status": "success" if schema_valid else "schema_error",
            "latency_seconds": time.perf_counter() - started,
            "schema_valid": schema_valid,
            "schema_errors": schema_errors,
            "claims": claims,
            "claim_count": len(claims),
            "should_memorize": parsed.get("should_memorize") if isinstance(parsed, dict) else None,
            "finish_reason": choice.get("finish_reason"),
            "raw_request_id": envelope.get("id") or response.headers.get("x-request-id"),
            "usage": usage,
            "cost_cny": usage_cost_cny(usage),
            "cost_known": True,
            "duplicate_profile": base.duplicate_profile(claims),
        }
    except Exception as error:
        status_code = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
        provider_body = error.response.text[:500] if isinstance(error, httpx.HTTPStatusError) else None
        return {
            **record,
            "completed_at": base.utc_now(),
            "request_started": request_dispatched,
            "attempt_count": 1 if request_dispatched else 0,
            "status": "api_error" if request_dispatched else "runner_error",
            "latency_seconds": time.perf_counter() - started,
            "schema_valid": False,
            "schema_errors": [f"{type(error).__name__}: {str(error)[:300]}"],
            "claims": [],
            "claim_count": 0,
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
            },
            "cost_cny": 0.0,
            "cost_known": not request_dispatched,
            "duplicate_profile": base.duplicate_profile([]),
            "error": {
                "class": type(error).__name__,
                "status_code": status_code,
                "provider_body": provider_body,
            },
        }


def _load_gold_source(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("gold source is not an object")
    return value


def run_experiment(
    manifest_path: Path,
    env_file: Path,
    gold_source_path: Path,
    output_path: Path,
    metadata_path: Path,
) -> dict[str, Any]:
    if output_path.exists() and output_path.stat().st_size:
        raise FileExistsError(f"refusing to repeat frozen calls; output already exists: {output_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cases = [case for case in manifest.get("cases", []) if case.get("case_type") == "dense"]
    if len(cases) != 20 or len({str(case["case_id"]) for case in cases}) != 20:
        raise ValueError("frozen arm D requires exactly 20 unique dense cases")
    runtimes = {str(case["case_id"]): _runtime_for_dense_case(manifest, case) for case in cases}
    for case in cases:
        runtime = runtimes[str(case["case_id"])]
        payload = build_payload(runtime["messages"], language=str(runtime["language"]))
        forbidden = {"max_tokens", "enable_thinking", "thinking_budget"}.intersection(payload)
        if forbidden:
            raise ValueError(f"forbidden GLM payload keys: {sorted(forbidden)}")
    api_key = base._env_value(env_file, "LLM_API_KEY")
    if not api_key:
        raise ValueError("LLM_API_KEY is empty")
    gold_source = _load_gold_source(gold_source_path)
    manifest_sha = base.file_sha256(manifest_path)
    gold_sha = base.gold_definition_sha256(gold_source)
    metadata: dict[str, Any] = {
        "protocol_id": PROTOCOL_ID,
        "started_at": base.utc_now(),
        "status": "running",
        "manifest_sha256": manifest_sha,
        "gold_definition_sha256": gold_sha,
        "base_runner_sha256": base.file_sha256(BASE_RUNNER_PATH),
        "runner_sha256": base.file_sha256(Path(__file__)),
        "configuration": {
            "arm": ARM,
            "source_prompt_arm": SOURCE_PROMPT_ARM,
            "provider": "zhipu",
            "model": MODEL,
            "base_url": BASE_URL,
            "thinking": dict(THINKING),
            "reasoning_effort": REASONING_EFFORT,
            "schema_max_items": 30,
            "max_tokens": None,
            "enable_thinking": None,
            "case_type": "dense",
            "expected_case_count": 20,
            "max_attempts": MAX_ATTEMPTS,
            "concurrency": CONCURRENCY,
            "timeout_seconds": TIMEOUT_SECONDS,
            "pricing_cny_per_million": {
                "input": INPUT_PRICE_PER_MILLION,
                "output": OUTPUT_PRICE_PER_MILLION,
                "discount": "5折实测",
            },
        },
    }
    base.write_json(metadata_path, metadata)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_lock = threading.Lock()
    rows: list[dict[str, Any]] = []
    with httpx.Client() as client, ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
        futures = {
            executor.submit(_execute_case, client, api_key, case, runtimes[str(case["case_id"])]): str(case["case_id"])
            for case in cases
        }
        for future in as_completed(futures):
            row = future.result()
            rows.append(row)
            with write_lock, output_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                handle.flush()
            print(
                json.dumps(
                    {
                        "case_id": row["case_id"][:8],
                        "status": row["status"],
                        "latency_seconds": round(float(row["latency_seconds"]), 3),
                        "claims": row["claim_count"],
                        "reasoning_tokens": row["usage"]["reasoning_tokens"],
                        "cost_cny": round(float(row["cost_cny"]), 6),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    metadata.update(
        {
            "completed_at": base.utc_now(),
            "status": "completed",
            "records": len(rows),
            "final_case_records": len({str(row["case_id"]) for row in rows}),
            "requests_started": sum(bool(row.get("request_started")) for row in rows),
            "max_attempt_count": max((int(row.get("attempt_count") or 0) for row in rows), default=0),
            "cost_known_records": sum(bool(row.get("cost_known")) for row in rows),
            "cost_total_cny": sum(float(row.get("cost_cny") or 0.0) for row in rows),
            "runs_sha256": base.file_sha256(output_path),
        }
    )
    base.write_json(metadata_path, metadata)
    return metadata


def prepare_review_template(
    manifest_path: Path,
    runs_path: Path,
    gold_source_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    if output_path.exists():
        raise FileExistsError(f"refusing to overwrite manual review: {output_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    runs = base.read_jsonl(runs_path)
    latest = {str(row.get("case_id")): row for row in runs if row.get("arm") == ARM}
    gold_source = _load_gold_source(gold_source_path)
    source_review = gold_source.get("key_fact_review")
    if not isinstance(source_review, Mapping):
        raise ValueError("gold source key_fact_review is missing")
    source_cases = [case for case in source_review.get("cases", []) if isinstance(case, Mapping)]
    expected_ids = [str(value) for value in manifest.get("selection", {}).get("key_fact_case_ids", [])]
    by_id = {str(case.get("case_id")): case for case in source_cases}
    if len(expected_ids) != 5 or set(expected_ids) != set(by_id):
        raise ValueError("gold source does not match the frozen five cases")
    review_cases = []
    for case_id in expected_ids:
        source_case = by_id[case_id]
        row = latest.get(case_id)
        if row is None:
            raise ValueError(f"arm D run is missing review case: {case_id}")
        claims = [claim for claim in row.get("claims", []) if isinstance(claim, Mapping)]
        review_cases.append(
            {
                "case_id": case_id,
                "reviewed": False,
                "coverage_gate_included": bool(source_case.get("coverage_gate_included", case_id != expected_ids[0])),
                "facts": [
                    {
                        "id": fact.get("id"),
                        "text": fact.get("text"),
                        "covered": None,
                        "covered_by_claim_indices": [],
                    }
                    for fact in source_case.get("facts", [])
                    if isinstance(fact, Mapping)
                ],
                "claim_count": len(claims),
                "claims": [
                    {
                        "claim_index": index,
                        "subject": claim.get("subject"),
                        "value": claim.get("value"),
                        "kind": claim.get("kind"),
                        "evidence_quote": claim.get("evidence_quote"),
                    }
                    for index, claim in enumerate(claims)
                ],
                "hallucinations": [],
                "subject_misbindings": [],
            }
        )
    review = {
        "protocol_id": PROTOCOL_ID,
        "manifest_sha256": base.file_sha256(manifest_path),
        "gold_definition_sha256": base.gold_definition_sha256(gold_source),
        "taxonomy": {
            "hallucination": "fabricated claim not supported by the source",
            "subject_misbinding": "source-supported fact assigned to the wrong subject; reported separately and excluded from hallucination",
        },
        "key_fact_review": {
            "case_ids": expected_ids,
            "reviewed_case_count": 0,
            "coverage_case_count": 4,
            "cases": review_cases,
        },
        "short_event_review": deepcopy(gold_source.get("short_event_review", {})),
    }
    base.write_json(output_path, review)
    return review


def _manual_review_metrics(manifest: Mapping[str, Any], review: Mapping[str, Any]) -> dict[str, Any]:
    expected_ids = [str(value) for value in manifest.get("selection", {}).get("key_fact_case_ids", [])]
    key_review = review.get("key_fact_review") if isinstance(review.get("key_fact_review"), Mapping) else {}
    cases = [case for case in key_review.get("cases", []) if isinstance(case, Mapping)]
    ids = [str(case.get("case_id")) for case in cases]
    details_valid = len(expected_ids) == 5 and len(cases) == 5 and len(set(ids)) == 5 and set(ids) == set(expected_ids)
    reviewed_cases = 0
    included_cases = 0
    covered = 0
    total = 0
    hallucinations = 0
    subject_misbindings = 0
    for case in cases:
        if case.get("reviewed") is True:
            reviewed_cases += 1
        else:
            details_valid = False
        facts = [fact for fact in case.get("facts", []) if isinstance(fact, Mapping)]
        if not facts or any(not isinstance(fact.get("covered"), bool) for fact in facts):
            details_valid = False
        if case.get("coverage_gate_included") is True:
            included_cases += 1
            total += len(facts)
            covered += sum(fact.get("covered") is True for fact in facts)
        case_hallucinations = case.get("hallucinations")
        case_misbindings = case.get("subject_misbindings")
        if not isinstance(case_hallucinations, list) or not isinstance(case_misbindings, list):
            details_valid = False
        else:
            hallucinations += len(case_hallucinations)
            subject_misbindings += len(case_misbindings)
    details_valid = details_valid and included_cases == 4 and reviewed_cases == 5
    return {
        "reviewed_cases": reviewed_cases,
        "coverage_case_count": included_cases,
        "covered": covered,
        "total": total,
        "coverage": covered / total if total else 0.0,
        "hallucinated_claims": hallucinations,
        "subject_misbindings": subject_misbindings,
        "details_valid": details_valid,
    }


def _gate(gate_id: str, measured: Any, threshold: str, passed: bool, *, diagnostic: bool = False) -> dict[str, Any]:
    return {
        "gate_id": gate_id,
        "measured": measured,
        "threshold": threshold,
        "passed": bool(passed),
        "diagnostic_only": diagnostic,
    }


def score_records(
    manifest: Mapping[str, Any],
    runs: Sequence[Mapping[str, Any]],
    manual_review: Mapping[str, Any],
) -> dict[str, Any]:
    cases = [case for case in manifest.get("cases", []) if case.get("case_type") == "dense"]
    expected_ids = {str(case["case_id"]) for case in cases}
    d_runs = [row for row in runs if row.get("arm") == ARM and str(row.get("case_id")) in expected_ids]
    latest: dict[str, Mapping[str, Any]] = {}
    for row in d_runs:
        latest[str(row.get("case_id"))] = row
    rows = [latest[case_id] for case_id in expected_ids if case_id in latest]
    attempted = [row for row in rows if row.get("request_started")]
    valid = [row for row in rows if row.get("schema_valid")]
    latencies = [float(row.get("latency_seconds") or 0.0) for row in attempted]
    p50 = base._percentile(latencies, 0.50)
    p95 = base._percentile(latencies, 0.95)
    qualifying = sum(int(row.get("claim_count") or 0) >= 12 and bool(row.get("schema_valid")) for row in rows)
    failed_cases = [
        {"case_id": str(row.get("case_id")), "claims": int(row.get("claim_count") or 0)}
        for row in rows
        if not row.get("schema_valid") or int(row.get("claim_count") or 0) < 12
    ]
    reasoning = [int(row.get("usage", {}).get("reasoning_tokens") or 0) for row in rows]
    total_cost = sum(float(row.get("cost_cny") or 0.0) for row in rows)
    mean_cost = total_cost / len(cases) if cases else None
    duplicate_count = sum(int(base._row_duplicate_profile(row).get("duplicate_count") or 0) for row in rows)
    duplicate_claims = sum(int(base._row_duplicate_profile(row).get("claim_count") or 0) for row in rows)
    duplicate_rate = duplicate_count / duplicate_claims if duplicate_claims else 0.0
    review = _manual_review_metrics(manifest, manual_review)
    case_attempts: dict[str, int] = {}
    for row in d_runs:
        case_id = str(row.get("case_id"))
        case_attempts[case_id] = case_attempts.get(case_id, 0) + int(bool(row.get("request_started")))
    gates = [
        _gate(
            "density",
            {"qualifying_cases": qualifying, "cases": len(cases), "failed_cases": sorted(failed_cases, key=lambda item: item["case_id"])},
            ">=18/20 claims>=12",
            len(cases) == 20 and qualifying >= 18,
        ),
        _gate("latency_p50", p50, "<=40s", len(attempted) == 20 and p50 is not None and p50 <= 40.0),
        _gate("latency_p95", p95, "<=90s", len(attempted) == 20 and p95 is not None and p95 <= 90.0),
        _gate(
            "reasoning_tokens",
            {"max": max(reasoning, default=None), "over_1024": sum(value > 1024 for value in reasoning)},
            "all <=1024",
            len(reasoning) == 20 and all(value <= 1024 for value in reasoning),
        ),
        _gate(
            "cost_mean",
            mean_cost,
            "<=¥0.02/case",
            len(rows) == 20 and all(row.get("cost_known") is not False for row in rows) and mean_cost is not None and mean_cost <= 0.02,
        ),
        _gate("schema_success", len(valid) / len(cases) if cases else 0.0, ">=95%", len(valid) / len(cases) >= 0.95 if cases else False),
        _gate(
            "duplicate_rate",
            {"duplicates": duplicate_count, "claims": duplicate_claims, "rate": duplicate_rate},
            "<=2%",
            len(rows) == 20 and duplicate_rate <= 0.02,
        ),
        _gate(
            "hallucination",
            {"reviewed_cases": review["reviewed_cases"], "hallucinated_claims": review["hallucinated_claims"]},
            "0 fabricated claims in same 5-case gold review",
            review["details_valid"] and review["hallucinated_claims"] == 0,
        ),
        _gate(
            "subject_misbinding",
            {"reviewed_cases": review["reviewed_cases"], "claims": review["subject_misbindings"]},
            "diagnostic only; excluded from hallucination gate",
            True,
            diagnostic=True,
        ),
        _gate(
            "gold_coverage_corrected",
            {"covered": review["covered"], "total": review["total"], "coverage": review["coverage"]},
            ">=90% on 4 non-boundary cases",
            review["details_valid"] and review["total"] == 40 and review["coverage"] >= 0.90,
        ),
        _gate(
            "run_integrity",
            {
                "actual_api_calls": len(attempted),
                "final_cases": len(rows),
                "max_actual_calls_per_case": max(case_attempts.values(), default=0),
            },
            "20 calls, 20 final cases, exactly 1 actual call/case",
            len(d_runs) == 20 and len(rows) == 20 and set(case_attempts) == expected_ids and all(value == 1 for value in case_attempts.values()),
        ),
    ]
    overall_passed = all(gate["passed"] for gate in gates if not gate.get("diagnostic_only"))
    claim_total = sum(int(row.get("claim_count") or 0) for row in rows)
    return {
        "protocol_id": PROTOCOL_ID,
        "overall_passed": overall_passed,
        "gates": gates,
        "arm_d": {
            "cases": len(rows),
            "claims_ge_12": qualifying,
            "claim_total": claim_total,
            "claim_mean": claim_total / len(cases) if cases else None,
            "latency_p50_seconds": p50,
            "latency_p95_seconds": p95,
            "reasoning_tokens_max": max(reasoning, default=None),
            "reasoning_tokens_mean": sum(reasoning) / len(reasoning) if reasoning else None,
            "cost_total_cny": total_cost,
            "cost_mean_cny": mean_cost,
            "schema_success_rate": len(valid) / len(cases) if cases else 0.0,
            "duplicate_count": duplicate_count,
            "duplicate_rate": duplicate_rate,
            "gold_review": {
                "covered": review["covered"],
                "total": review["total"],
                "coverage": review["coverage"],
                "hallucinated_claims": review["hallucinated_claims"],
                "subject_misbindings": review["subject_misbindings"],
            },
            "configuration": {
                "provider": "zhipu",
                "model": MODEL,
                "prompt": "B_density_max30",
                "thinking": dict(THINKING),
                "reasoning_effort": REASONING_EFFORT,
                "max_tokens": None,
                "enable_thinking": None,
                "timeout_seconds": TIMEOUT_SECONDS,
                "max_attempts": MAX_ATTEMPTS,
                "concurrency": CONCURRENCY,
            },
        },
        "cost": {
            "actual_cny": total_cost,
            "mean_cny": mean_cost,
            "all_costs_known": len(rows) == 20 and all(row.get("cost_known") is not False for row in rows),
        },
        "pricing": {
            "input_cny_per_million": INPUT_PRICE_PER_MILLION,
            "output_cny_per_million": OUTPUT_PRICE_PER_MILLION,
            "basis": "智谱按量 ¥0.4/百万输入 tokens、¥1.4/百万输出 tokens（5折）实测 usage",
        },
    }


def _c1024_adjusted(report: Mapping[str, Any]) -> dict[str, Any]:
    arm = report.get("arms_dense_only", {}).get("C", {})
    gold = arm.get("gold_review", {}) if isinstance(arm, Mapping) else {}
    return {
        "overall_passed": False,
        "claims_ge_12": arm.get("claims_ge_12"),
        "claim_total": arm.get("claim_total"),
        "claim_mean": arm.get("claim_mean"),
        "latency_p50_seconds": arm.get("latency_p50_seconds"),
        "latency_p95_seconds": arm.get("latency_p95_seconds"),
        "reasoning_tokens_max": arm.get("reasoning_tokens_max"),
        "reasoning_tokens_mean": arm.get("reasoning_tokens_mean"),
        "cost_total_cny": arm.get("cost_total_cny"),
        "cost_mean_cny": arm.get("cost_mean_cny"),
        "schema_success_rate": arm.get("schema_success_rate"),
        "duplicate_count": arm.get("duplicate_count"),
        "duplicate_rate": arm.get("duplicate_rate"),
        "coverage": gold.get("coverage"),
        "hallucinated_claims": 0,
        "subject_misbindings": 13,
        "taxonomy_adjustment": "C-1024 necropsy reclassified the 13 prior hallucination labels as subject misbindings, not fabricated claims",
    }


def _production_landing_diff() -> dict[str, Any]:
    toml_lines = [
        'provider = "zhipu"',
        'model = "glm-5.3-flash"',
        'reasoning_effort = "low"',
    ]
    return {
        "prompt_additions_zh": list(base.ZH_DENSITY_LINES),
        "prompt_additions_en": list(base.EN_DENSITY_LINES),
        "six_scalars": {
            "schema.claims.maxItems": 30,
            "llm.thinking.type": "enabled",
            "llm.reasoning_effort": "low",
            "llm.timeout_seconds": 120,
            "llm.max_attempts": 1,
            "llm.concurrency": 4,
        },
        "toml_lines": {"volcano": toml_lines, "local": toml_lines},
        "provider_code_followup": "ZhipuProvider adds one optional reasoning_effort argument and forwards it at the top level; production code change is a separate implementation batch.",
    }


def write_gate_table(path: Path, report: Mapping[str, Any]) -> None:
    lines = [
        "# GLM effort=low 20案终验门禁",
        "",
        f"- 总判定：{'PASS' if report['overall_passed'] else 'FAIL'}",
        f"- 成本实耗：¥{report['cost']['actual_cny']:.6f}；均价 ¥{report['cost']['mean_cny']:.6f}/案",
        "- GLM 价格口径：¥0.4/百万输入 tokens、¥1.4/百万输出 tokens（5折）实测",
        "- 主语误绑单独报告，不并入虚构 claim。",
        "",
        "| 门禁 | 实测 | 线 | 判定 |",
        "|---|---:|---:|:---:|",
    ]
    for gate in report["gates"]:
        measured = json.dumps(gate["measured"], ensure_ascii=False, separators=(",", ":")) if not isinstance(gate["measured"], float) else f"{gate['measured']:.9f}"
        verdict = "INFO" if gate.get("diagnostic_only") else ("PASS" if gate["passed"] else "FAIL")
        lines.append(f"| {gate['gate_id']} | {measured} | {gate['threshold']} | {verdict} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_comparison(path: Path, d_report: Mapping[str, Any], c_adjusted: Mapping[str, Any]) -> None:
    d = d_report["arm_d"]
    rows = [
        ("overall_passed", d_report["overall_passed"], c_adjusted["overall_passed"]),
        ("claims_ge_12_of_20", d["claims_ge_12"], c_adjusted["claims_ge_12"]),
        ("claim_total", d["claim_total"], c_adjusted["claim_total"]),
        ("claim_mean", d["claim_mean"], c_adjusted["claim_mean"]),
        ("latency_p50_seconds", d["latency_p50_seconds"], c_adjusted["latency_p50_seconds"]),
        ("latency_p95_seconds", d["latency_p95_seconds"], c_adjusted["latency_p95_seconds"]),
        ("reasoning_tokens_max", d["reasoning_tokens_max"], c_adjusted["reasoning_tokens_max"]),
        ("reasoning_tokens_mean", d["reasoning_tokens_mean"], c_adjusted["reasoning_tokens_mean"]),
        ("cost_total_cny", d["cost_total_cny"], c_adjusted["cost_total_cny"]),
        ("cost_mean_cny", d["cost_mean_cny"], c_adjusted["cost_mean_cny"]),
        ("schema_success_rate", d["schema_success_rate"], c_adjusted["schema_success_rate"]),
        ("duplicate_count", d["duplicate_count"], c_adjusted["duplicate_count"]),
        ("duplicate_rate", d["duplicate_rate"], c_adjusted["duplicate_rate"]),
        ("gold_coverage_corrected", d["gold_review"]["coverage"], c_adjusted["coverage"]),
        ("hallucinated_claims_corrected", d["gold_review"]["hallucinated_claims"], c_adjusted["hallucinated_claims"]),
        ("subject_misbindings", d["gold_review"]["subject_misbindings"], c_adjusted["subject_misbindings"]),
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["metric", "D_glm_effort_low", "C_qwen_c1024"])
        writer.writerows(rows)


def score_experiment(
    manifest_path: Path,
    runs_path: Path,
    review_path: Path,
    metadata_path: Path,
    c_report_path: Path,
    output_path: Path,
    gate_table_path: Path,
    comparison_path: Path,
) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    runs = base.read_jsonl(runs_path)
    review = json.loads(review_path.read_text(encoding="utf-8"))
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    c_report = json.loads(c_report_path.read_text(encoding="utf-8"))
    report = score_records(manifest, runs, review)
    manifest_sha = base.file_sha256(manifest_path)
    gold_sha = base.gold_definition_sha256(review)
    integrity_mismatches = []
    if metadata.get("protocol_id") != PROTOCOL_ID:
        integrity_mismatches.append("metadata.protocol_id")
    if metadata.get("manifest_sha256") != manifest_sha or review.get("manifest_sha256") != manifest_sha:
        integrity_mismatches.append("manifest_sha256")
    if metadata.get("gold_definition_sha256") != gold_sha or review.get("gold_definition_sha256") != gold_sha:
        integrity_mismatches.append("gold_definition_sha256")
    if metadata.get("runs_sha256") != base.file_sha256(runs_path):
        integrity_mismatches.append("runs_sha256")
    integrity_gate = _gate(
        "freeze_integrity",
        {"mismatches": integrity_mismatches},
        "manifest/gold/runs hashes match pre-run freeze",
        not integrity_mismatches,
    )
    report["gates"].append(integrity_gate)
    report["overall_passed"] = bool(report["overall_passed"] and integrity_gate["passed"])
    report.update(
        {
            "scored_at": base.utc_now(),
            "disposition": (
                "PASS -> GLM effort=low is the production extraction configuration; production implementation remains a separate batch."
                if report["overall_passed"]
                else "FAIL -> report measured values and hand the terminal D vs C-1024 choice to the user without tuning or rerun."
            ),
            "integrity": {
                "manifest_sha256": manifest_sha,
                "gold_definition_sha256": gold_sha,
                "runs_sha256": base.file_sha256(runs_path),
                "run_metadata_sha256": base.file_sha256(metadata_path),
                "request_runner_sha256": metadata.get("runner_sha256"),
                "scoring_runner_sha256": base.file_sha256(Path(__file__)),
                "base_runner_sha256": base.file_sha256(BASE_RUNNER_PATH),
                "post_run_equipment_fix": "review template now preserves the untouched short-event gold definitions required by the frozen gold hash; request payload construction was unchanged",
            },
            "c1024_adjusted": _c1024_adjusted(c_report),
            "production_landing_diff": _production_landing_diff() if report["overall_passed"] else None,
        }
    )
    base.write_json(output_path, report)
    write_gate_table(gate_table_path, report)
    write_comparison(comparison_path, report, report["c1024_adjusted"])
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    run.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    run.add_argument("--gold-source", type=Path, default=DEFAULT_GOLD_SOURCE)
    run.add_argument("--output", type=Path, default=DEFAULT_RUNS)
    run.add_argument("--metadata", type=Path, default=DEFAULT_METADATA)
    review = subparsers.add_parser("prepare-review")
    review.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    review.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    review.add_argument("--gold-source", type=Path, default=DEFAULT_GOLD_SOURCE)
    review.add_argument("--output", type=Path, default=DEFAULT_REVIEW)
    score = subparsers.add_parser("score")
    score.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    score.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    score.add_argument("--review", type=Path, default=DEFAULT_REVIEW)
    score.add_argument("--metadata", type=Path, default=DEFAULT_METADATA)
    score.add_argument("--c-report", type=Path, default=DEFAULT_C_REPORT)
    score.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    score.add_argument("--gate-table", type=Path, default=DEFAULT_GATE_TABLE)
    score.add_argument("--comparison", type=Path, default=DEFAULT_COMPARISON)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        metadata = run_experiment(args.manifest, args.env_file, args.gold_source, args.output, args.metadata)
        print(json.dumps(metadata, ensure_ascii=False, indent=2))
        return 0
    if args.command == "prepare-review":
        review = prepare_review_template(args.manifest, args.runs, args.gold_source, args.output)
        print(json.dumps({"review_cases": len(review["key_fact_review"]["cases"]), "output": str(args.output)}, ensure_ascii=False))
        return 0
    if args.command == "score":
        report = score_experiment(
            args.manifest,
            args.runs,
            args.review,
            args.metadata,
            args.c_report,
            args.output,
            args.gate_table,
            args.comparison,
        )
        print(json.dumps({"overall_passed": report["overall_passed"], "cost": report["cost"]}, ensure_ascii=False))
        return 0 if report["overall_passed"] else 1
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
