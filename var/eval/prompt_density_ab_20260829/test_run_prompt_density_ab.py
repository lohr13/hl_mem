from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import httpx
import pytest

SCRIPT = Path(__file__).with_name("run_prompt_density_ab.py")


def load_runner() -> ModuleType:
    assert SCRIPT.is_file(), "run_prompt_density_ab.py must implement the frozen experiment"
    spec = importlib.util.spec_from_file_location("prompt_density_ab_runner", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_case_hash_changes_pair_order_without_random_runtime_state() -> None:
    runner = load_runner()

    assert runner.arm_order("alpha") == ("A", "B")
    assert runner.arm_order("beta") == ("B", "A")
    assert runner.arm_order("alpha") == ("A", "B")


def test_payload_freezes_strict_schema_and_only_b_adds_density_contract() -> None:
    runner = load_runner()
    messages = [{"role": "user", "content": "source"}]

    payload_a = runner.build_payload(messages, arm="A", language="zh")
    payload_b = runner.build_payload(messages, arm="B", language="zh")

    assert payload_a["model"] == payload_b["model"] == "qwen3.8-flash"
    assert payload_a["enable_thinking"] is payload_b["enable_thinking"] is False
    assert payload_a["max_tokens"] == payload_b["max_tokens"] == 6000
    assert payload_a["response_format"]["type"] == "json_schema"
    assert payload_a["response_format"]["json_schema"]["strict"] is True
    assert payload_a["response_format"]["json_schema"]["schema"]["properties"]["claims"]["maxItems"] == 20
    assert payload_b["response_format"]["json_schema"]["schema"]["properties"]["claims"]["maxItems"] == 30
    assert "高密度长文通常应产出 12–30 条" not in runner.system_prompt("zh", "A")
    assert "高密度长文通常应产出 12–30 条" in runner.system_prompt("zh", "B")
    assert "禁止为接近 12 或 30" in runner.system_prompt("zh", "B")
    assert runner.system_prompt("zh", "B") == runner.SYSTEM_PROMPT
    assert runner.system_prompt("zh", "B").count(runner.ZH_DENSITY_LINES[0]) == 1

    assert "dense long source will often yield 12–30 claims" not in runner.system_prompt("en", "A")
    assert "dense long source will often yield 12–30 claims" in runner.system_prompt("en", "B")
    assert "never repeat, fragment, pad, generalize, or invent" in runner.system_prompt("en", "B")
    assert runner.system_prompt("en", "B") == runner.ENGLISH_SYSTEM_PROMPT
    assert runner.system_prompt("en", "B").count(runner.EN_DENSITY_LINES[0]) == 1


def test_checked_in_manifest_matches_frozen_executable_configuration() -> None:
    runner = load_runner()
    manifest = json.loads(SCRIPT.with_name("manifest.json").read_text(encoding="utf-8"))

    runner.validate_frozen_configuration(manifest)


def test_budget_guard_stops_new_requests_at_soft_limit_and_reserves_hard_limit() -> None:
    runner = load_runner()
    guard = runner.BudgetGuard(soft_limit_cny=0.80, hard_limit_cny=1.00)

    assert guard.try_reserve("first", 0.80) is True
    guard.settle("first", 0.80)
    assert guard.try_reserve("after-soft", 0.01) is False

    other = runner.BudgetGuard(soft_limit_cny=0.80, hard_limit_cny=1.00)
    assert other.try_reserve("one", 0.60) is True
    assert other.try_reserve("two", 0.41) is False
    assert other.try_reserve("two", 0.40) is True


def test_budget_guard_halts_after_unknown_provider_usage() -> None:
    runner = load_runner()
    guard = runner.BudgetGuard(soft_limit_cny=0.80, hard_limit_cny=1.00)

    assert guard.try_reserve("unknown", 0.20) is True
    charged = guard.halt_unknown_cost("unknown", "provider response omitted usage")

    assert charged == pytest.approx(0.20)
    assert guard.try_reserve("after-unknown", 0.01) is False
    snapshot = guard.snapshot()
    assert snapshot["halted"] is True
    assert snapshot["integrity_error"] == "provider response omitted usage"


def test_budget_guard_rejects_cost_above_reservation_and_halts() -> None:
    runner = load_runner()
    guard = runner.BudgetGuard(soft_limit_cny=0.80, hard_limit_cny=1.00)

    assert guard.try_reserve("overrun", 0.20) is True
    with pytest.raises(ValueError, match="exceeds reservation"):
        guard.settle("overrun", 0.21)

    assert guard.try_reserve("after-overrun", 0.01) is False
    assert guard.snapshot()["halted"] is True


def test_response_usage_requires_provider_token_counts() -> None:
    runner = load_runner()

    with pytest.raises(ValueError, match="usage"):
        runner._response_usage({})
    with pytest.raises(ValueError, match="completion_tokens"):
        runner._response_usage({"usage": {"prompt_tokens": 10}})


def test_non_2xx_response_charges_reservation_and_halts_budget() -> None:
    runner = load_runner()
    guard = runner.BudgetGuard(soft_limit_cny=0.80, hard_limit_cny=1.00)

    class RejectingClient:
        def post(self, *args: object, **kwargs: object) -> httpx.Response:
            request = httpx.Request("POST", runner.ENDPOINT)
            return httpx.Response(429, request=request, text='{"message":"rate limited"}')

    runtime = {
        "messages": [{"role": "user", "content": "source"}],
        "language": "zh",
        "body_length": 6,
        "body_sha256": runner.sha256_text("source"),
    }
    record = runner._execute_arm(
        RejectingClient(),
        "unused",
        guard,
        {"case_id": "non-2xx", "case_type": "dense"},
        runtime,
        "A",
    )

    assert record["status"] == "api_error"
    assert record["cost_cny"] > 0
    assert record["budget"]["halted"] is True
    assert guard.try_reserve("later", 0.01) is False


def test_dense_sampler_keeps_rare_language_and_is_seed_deterministic() -> None:
    runner = load_runner()
    candidates = [
        {"case_id": f"zh-{index:02d}", "language": "zh", "body_length": 100 + index * 10} for index in range(23)
    ] + [
        {"case_id": "en-short", "language": "en", "body_length": 90},
        {"case_id": "en-mid", "language": "en", "body_length": 220},
        {"case_id": "en-long", "language": "en", "body_length": 900},
    ]

    first = runner.select_dense_cases(candidates, count=20, seed=20260829)
    second = runner.select_dense_cases(list(reversed(candidates)), count=20, seed=20260829)

    assert [item["case_id"] for item in first] == [item["case_id"] for item in second]
    assert len(first) == 20
    assert {item["case_id"] for item in first if item["language"] == "en"} == {
        "en-short",
        "en-mid",
        "en-long",
    }
    assert {item["length_quartile"] for item in first if item["language"] == "zh"} == {1, 2, 3, 4}


def _claim(index: int) -> dict[str, object]:
    return {
        "subject": f"subject-{index}",
        "value": f"fact-{index}",
        "kind": "fact",
        "confidence": 1.0,
        "notability": "medium",
        "assertion_kind": "observation",
        "evidence_quote": f"fact-{index}",
        "source_event_indices": [0],
    }


def _passing_fixture() -> tuple[dict[str, object], list[dict[str, object]], dict[str, object]]:
    cases = [{"case_id": f"dense-{index:02d}", "case_type": "dense"} for index in range(20)] + [
        {"case_id": f"short-{index:02d}", "case_type": "short"} for index in range(6)
    ]
    configuration = {
        "arms": {"A": {"max_items": 20}, "B": {"max_items": 30}},
        "model": "qwen3.8-flash",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "enable_thinking": False,
        "strict_json_schema": True,
        "max_tokens": 6000,
        "timeout_seconds": 90.0,
        "max_attempts": 1,
        "prompt_sha256": {
            "A": {"zh": "prompt-a-zh", "en": "prompt-a-en"},
            "B": {"zh": "prompt-b-zh", "en": "prompt-b-en"},
        },
        "schema_sha256": {"A": "schema-a", "B": "schema-b"},
    }
    manifest: dict[str, object] = {
        "cases": cases,
        "selection": {"key_fact_case_ids": [f"dense-{index:02d}" for index in range(5)]},
        "configuration": configuration,
    }
    runs: list[dict[str, object]] = []
    for case in cases:
        for arm in ("A", "B"):
            claims = [_claim(index) for index in range(12 if case["case_type"] == "dense" else 2)]
            runs.append(
                {
                    "case_id": case["case_id"],
                    "case_type": case["case_type"],
                    "arm": arm,
                    "status": "success",
                    "latency_seconds": 20.0,
                    "claims": claims,
                    "claim_count": len(claims),
                    "schema_valid": True,
                    "usage": {
                        "input_tokens": 1000,
                        "output_tokens": 1000,
                        "reasoning_tokens": 0,
                        "cached_tokens": 0,
                    },
                    "cost_cny": 0.004,
                    "language": "zh",
                    "configuration": {
                        "model": configuration["model"],
                        "base_url": configuration["base_url"],
                        "enable_thinking": configuration["enable_thinking"],
                        "strict_json_schema": configuration["strict_json_schema"],
                        "max_tokens": configuration["max_tokens"],
                        "timeout_seconds": configuration["timeout_seconds"],
                        "max_attempts": configuration["max_attempts"],
                        "schema_max_items": configuration["arms"][arm]["max_items"],  # type: ignore[index]
                        "prompt_sha256": configuration["prompt_sha256"][arm]["zh"],  # type: ignore[index]
                        "schema_sha256": configuration["schema_sha256"][arm],  # type: ignore[index]
                    },
                }
            )
    key_cases = []
    for index in range(5):
        key_cases.append(
            {
                "case_id": f"dense-{index:02d}",
                "reviewed": True,
                "facts": [
                    {"id": f"f{fact:02d}", "text": f"gold-{index}-{fact}", "covered": fact < 9} for fact in range(10)
                ],
                "hallucinations": [],
            }
        )
    manual = {
        "key_fact_review": {
            "case_ids": [f"dense-{index:02d}" for index in range(5)],
            "reviewed_case_count": 5,
            "covered": 45,
            "total": 50,
            "hallucinated_claims": 0,
            "cases": key_cases,
        },
        "short_event_review": {
            "case_ids": [f"short-{index:02d}" for index in range(6)],
            "reviewed_case_count": 6,
            "padding_claims": 0,
            "cases": [
                {
                    "case_id": f"short-{index:02d}",
                    "reviewed": True,
                    "expected": f"short fact {index}",
                    "padding_claim_ids": [],
                }
                for index in range(6)
            ],
        },
    }
    return manifest, runs, manual


def test_score_passes_only_when_every_frozen_gate_passes() -> None:
    runner = load_runner()
    manifest, runs, manual = _passing_fixture()

    report = runner.score_records(manifest, runs, manual)

    assert report["overall_passed"] is True
    assert all(gate["passed"] for gate in report["gates"])
    measured = {gate["gate_id"]: gate["measured"] for gate in report["gates"]}
    assert measured["dense_12_claims"]["qualifying_cases"] == 20
    assert measured["requests_per_arm_case"] == pytest.approx(1.0)
    assert measured["key_fact_quality"]["coverage"] == pytest.approx(0.9)


def test_score_fails_closed_on_nonzero_reasoning() -> None:
    runner = load_runner()
    manifest, runs, manual = _passing_fixture()
    runs[1]["usage"]["reasoning_tokens"] = 1  # type: ignore[index]

    report = runner.score_records(manifest, runs, manual)

    gate = next(item for item in report["gates"] if item["gate_id"] == "reasoning_tokens")
    assert gate["passed"] is False
    assert report["overall_passed"] is False


def test_score_derives_manual_counts_and_rejects_tampered_aggregates() -> None:
    runner = load_runner()
    manifest, runs, manual = _passing_fixture()
    for case in manual["key_fact_review"]["cases"]:  # type: ignore[index]
        for fact in case["facts"]:  # type: ignore[index]
            fact["covered"] = int(fact["id"][1:]) < 5  # type: ignore[index]
    manual["key_fact_review"]["covered"] = 50  # type: ignore[index]

    report = runner.score_records(manifest, runs, manual)

    gate = next(item for item in report["gates"] if item["gate_id"] == "key_fact_quality")
    assert gate["measured"]["covered"] == 25
    assert gate["measured"]["coverage"] == pytest.approx(0.5)
    assert gate["measured"]["aggregate_consistent"] is False
    assert gate["passed"] is False


def test_score_fails_when_record_configuration_differs_from_manifest() -> None:
    runner = load_runner()
    manifest, runs, manual = _passing_fixture()
    runs[0]["configuration"]["prompt_sha256"] = "tampered"  # type: ignore[index]

    report = runner.score_records(manifest, runs, manual)

    gate = next(item for item in report["gates"] if item["gate_id"] == "configuration_integrity")
    assert gate["passed"] is False
    assert gate["measured"]["mismatch_count"] == 1


def test_run_configuration_validator_detects_prompt_drift() -> None:
    runner = load_runner()
    configuration = runner.frozen_configuration()
    configuration["prompt_sha256"]["B"]["zh"] = "tampered"
    manifest = {"configuration": configuration}

    with pytest.raises(ValueError, match="configuration"):
        runner.validate_frozen_configuration(manifest)


def test_metadata_integrity_fails_closed_on_missing_or_wrong_binding() -> None:
    runner = load_runner()
    manual = {"manifest_sha256": "manifest-hash"}

    missing = runner.run_metadata_integrity(None, "manifest-hash", manual, "gold-hash")
    mismatched = runner.run_metadata_integrity(
        {
            "protocol_id": "wrong-protocol",
            "manifest_sha256": "wrong-manifest",
            "gold_definition_sha256": "wrong-gold",
        },
        "manifest-hash",
        manual,
        "gold-hash",
    )

    assert missing["passed"] is False
    assert "metadata_missing" in missing["mismatches"]
    assert mismatched["passed"] is False
    assert set(mismatched["mismatches"]) == {
        "metadata.protocol_id",
        "metadata.manifest_sha256",
        "metadata.gold_definition_sha256",
    }


def test_score_writes_failed_metadata_gate_when_metadata_file_is_missing(tmp_path: Path) -> None:
    runner = load_runner()
    manifest, runs, manual = _passing_fixture()
    manifest["protocol_id"] = runner.PROTOCOL_ID
    manifest_path = tmp_path / "manifest.json"
    runs_path = tmp_path / "runs.jsonl"
    manual_path = tmp_path / "manual.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manual["manifest_sha256"] = runner.file_sha256(manifest_path)
    manual_path.write_text(json.dumps(manual), encoding="utf-8")
    runs_path.write_text("".join(json.dumps(row) + "\n" for row in runs), encoding="utf-8")

    report = runner.score_experiment(
        manifest_path,
        runs_path,
        manual_path,
        tmp_path / "missing-metadata.json",
        tmp_path / "report.json",
        tmp_path / "gates.md",
        tmp_path / "comparison.csv",
    )

    gate = next(item for item in report["gates"] if item["gate_id"] == "run_metadata_integrity")
    assert gate["passed"] is False
    assert report["overall_passed"] is False


def test_normalized_duplicate_profile_counts_repeated_claim_once() -> None:
    runner = load_runner()
    claims = [_claim(1), _claim(2), {**_claim(1), "value": "  FACT-1  "}]

    profile = runner.duplicate_profile(claims)

    assert profile == {"claim_count": 3, "duplicate_count": 1, "duplicate_rate": pytest.approx(1 / 3)}


def test_cli_help_exposes_prepare_run_and_score() -> None:
    runner = load_runner()

    parser = runner.build_parser()
    help_text = parser.format_help()

    assert "prepare" in help_text
    assert "run" in help_text
    assert "score" in help_text

    args = parser.parse_args(["score", "--metadata", "custom-metadata.json"])
    assert args.metadata == Path("custom-metadata.json")
