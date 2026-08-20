"""Budgeted, resumable orchestration for v0.29.1 behavioral evaluation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.run_v0291_injection_replay import (
    build_fixture,
    load_fixture_spec,
    run_replay,
    write_expanded_fixture,
)

from .agent import (
    AGENT_FINAL_SYSTEM_PROMPT,
    AGENT_SYSTEM_PROMPT,
    AgentTraceGenerator,
    ModelCallResult,
    StructuredModelTransport,
    build_blind_agent_input,
    input_sha256,
)
from .aggregate import aggregate_behavioral_results
from .manifest import expand_behavioral_samples, load_behavioral_manifest
from .packet import materialize_behavioral_arms
from .scorer import (
    JUDGE_SCHEMA,
    JUDGE_SYSTEM_PROMPT,
    MODEL_SNAPSHOT,
    BehavioralScorer,
    build_judge_input,
    build_judge_schema,
    load_sentinels,
    sentinel_mismatches,
)

INPUT_CNY_PER_MILLION = 2.0
OUTPUT_CNY_PER_MILLION = 8.0
HARD_BUDGET_CNY = 15.0


class BudgetExceeded(RuntimeError):
    """A call could exceed the conservative hard CNY ceiling."""


class GateBlocked(RuntimeError):
    """A prerequisite gate forbids the paid behavioral phase."""


class DuplicateRecord(ValueError):
    """A resumable artifact contains duplicate valid exact keys."""


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class BudgetLedger:
    """Reserve worst-case cost before calls and reconcile provider usage atomically."""

    def __init__(self, *, hard_budget_cny: float = HARD_BUDGET_CNY) -> None:
        if not 0 < hard_budget_cny <= HARD_BUDGET_CNY:
            raise ValueError(f"hard budget must be within (0, {HARD_BUDGET_CNY}] CNY")
        self.hard_budget_cny = hard_budget_cny
        self._lock = asyncio.Lock()
        self._counter = 0
        self._reservations: dict[str, float] = {}
        self._spent_cny = 0.0
        self._actual_input_tokens = 0
        self._actual_output_tokens = 0
        self._conservative_charged_cny = 0.0
        self._charged_reservation_failures = 0

    async def reserve(
        self,
        *,
        system_prompt: str,
        user_payload: Mapping[str, Any],
        max_output_tokens: int,
    ) -> str:
        if max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        input_upper_bound = len((system_prompt + _canonical_json(user_payload)).encode("utf-8"))
        reservation_cost = (
            input_upper_bound * INPUT_CNY_PER_MILLION + max_output_tokens * OUTPUT_CNY_PER_MILLION
        ) / 1_000_000
        async with self._lock:
            projected = self._spent_cny + sum(self._reservations.values()) + reservation_cost
            if projected > self.hard_budget_cny:
                raise BudgetExceeded(f"projected cost ¥{projected:.6f} exceeds hard budget ¥{self.hard_budget_cny:.2f}")
            self._counter += 1
            reservation_id = f"reservation-{self._counter}"
            self._reservations[reservation_id] = reservation_cost
            return reservation_id

    async def reconcile(
        self,
        reservation_id: str,
        *,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        actual_cost = (input_tokens * INPUT_CNY_PER_MILLION + output_tokens * OUTPUT_CNY_PER_MILLION) / 1_000_000
        async with self._lock:
            if reservation_id not in self._reservations:
                raise KeyError(f"unknown budget reservation: {reservation_id}")
            reserved = self._reservations.pop(reservation_id)
            if actual_cost > reserved + 1e-12:
                raise BudgetExceeded(f"provider usage cost ¥{actual_cost:.6f} exceeded reservation ¥{reserved:.6f}")
            self._spent_cny += actual_cost
            self._actual_input_tokens += input_tokens
            self._actual_output_tokens += output_tokens

    async def charge_reserved(self, reservation_id: str, reason: str) -> None:
        """Charge the full reservation when response usage is unknowable."""

        if not reason:
            raise ValueError("reservation failure reason must be non-empty")
        async with self._lock:
            if reservation_id not in self._reservations:
                raise KeyError(f"unknown budget reservation: {reservation_id}")
            reserved = self._reservations.pop(reservation_id)
            self._spent_cny += reserved
            self._conservative_charged_cny += reserved
            self._charged_reservation_failures += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "hard_budget_cny": self.hard_budget_cny,
            "spent_cny": self._spent_cny,
            "reserved_cny": sum(self._reservations.values()),
            "outstanding_reservations": len(self._reservations),
            "actual_input_tokens": self._actual_input_tokens,
            "actual_output_tokens": self._actual_output_tokens,
            "conservative_charged_cny": self._conservative_charged_cny,
            "charged_reservation_failures": self._charged_reservation_failures,
        }


class BudgetedTransport(StructuredModelTransport):
    """Wrap every model call in a conservative ledger reservation."""

    def __init__(
        self,
        transport: StructuredModelTransport,
        ledger: BudgetLedger,
    ) -> None:
        self.transport = transport
        self.ledger = ledger

    async def complete(
        self,
        *,
        system_prompt: str,
        user_payload: Mapping[str, Any],
        schema_name: str,
        response_schema: Mapping[str, Any],
        max_output_tokens: int,
    ) -> ModelCallResult:
        reservation = await self.ledger.reserve(
            system_prompt=system_prompt,
            user_payload=user_payload,
            max_output_tokens=max_output_tokens,
        )
        try:
            result = await self.transport.complete(
                system_prompt=system_prompt,
                user_payload=user_payload,
                schema_name=schema_name,
                response_schema=response_schema,
                max_output_tokens=max_output_tokens,
            )
        except Exception as error:
            await self.ledger.charge_reserved(reservation, type(error).__name__)
            raise
        await self.ledger.reconcile(
            reservation,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )
        return result


def prepare_agent_assignments(
    manifest: Mapping[str, Any],
    samples: Sequence[Mapping[str, Any]],
    packet_assignments: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Materialize exact blind hashes and reuse only byte-identical invocations."""

    samples_by_id = {str(sample["opaque_sample_id"]): sample for sample in samples}
    assignments: list[dict[str, Any]] = []
    unique_inputs: dict[str, dict[str, Any]] = {}
    for packet in packet_assignments:
        sample_id = str(packet["opaque_sample_id"])
        sample = samples_by_id[sample_id]
        blind = build_blind_agent_input(manifest, sample, packet)
        digest = input_sha256(blind)
        existing = unique_inputs.get(digest)
        if existing is not None and existing != blind:
            raise ValueError(f"SHA-256 collision for blind input: {digest}")
        unique_inputs[digest] = blind
        assignments.append(
            {
                "opaque_sample_id": sample_id,
                "scenario_family_id": sample["scenario_family_id"],
                "cohort": sample["cohort"],
                "arm_name": packet["arm_name"],
                "applicable_dimensions": list(sample["applicable_dimensions"]),
                "context_packet_text": packet["context_packet_text"],
                "agent_input_sha256": digest,
                "blind_input": blind,
            }
        )
    return assignments, unique_inputs


def require_sentinel_gate(path: Path) -> dict[str, Any]:
    """Require all nine valid and matching sentinel judgments before full spend."""

    raw_payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw_payload, dict):
        raise GateBlocked("sentinel scorer artifact must be a JSON object")
    payload: dict[str, Any] = raw_payload
    if payload.get("passed") is not True or payload.get("valid_count") != 9 or payload.get("matched_count") != 9:
        raise GateBlocked("sentinel scorer gate requires valid and matching 9/9")
    return payload


def load_unique_jsonl(
    path: Path,
    *,
    key_field: str,
    valid_status: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Load resumable records and reject duplicate valid exact keys."""

    if not path.is_file():
        return {}
    records: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        payload = json.loads(line)
        key = str(payload[key_field])
        is_valid = valid_status is None or payload.get("call_status") == valid_status
        if is_valid and key in records:
            raise DuplicateRecord(f"duplicate valid key {key} at line {line_number}")
        if is_valid:
            records[key] = payload
    return records


def _judge_result_key(scored: Mapping[str, Any]) -> str:
    """Identify a result by every scorer input that can change its judgment."""

    return _sha256_object(
        {
            "input_sha256": scored["input_sha256"],
            "prompt_sha256": scored["prompt_sha256"],
            "schema_sha256": scored["schema_sha256"],
            "model": scored["model"],
        }
    )


def _select_current_judge_records(
    judge_records: Mapping[str, Mapping[str, Any]],
    representative_sample: Mapping[str, Mapping[str, Any]],
    agent_records: Mapping[str, Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    """Reuse only successful records produced by the current scorer identity."""

    selected: dict[str, Mapping[str, Any]] = {}
    for record in judge_records.values():
        digest = str(record.get("agent_input_sha256", ""))
        if digest not in representative_sample or digest not in agent_records:
            continue
        sample = representative_sample[digest]
        trace = agent_records[digest]["trace"]
        expected = {
            "input_sha256": _sha256_object(build_judge_input(sample, trace)),
            "prompt_sha256": _sha256_text(JUDGE_SYSTEM_PROMPT),
            "schema_sha256": _sha256_object(
                build_judge_schema(sample["applicable_dimensions"], missing_trace=not trace)
            ),
            "model": MODEL_SNAPSHOT,
        }
        if all(record.get(field) == value for field, value in expected.items()):
            selected[digest] = record
    return selected


def run_structural_phase(fixture_path: Path, output_dir: Path) -> dict[str, Any]:
    """Run and freeze the complete zero-cost 200×4 structural replay."""

    spec = load_fixture_spec(fixture_path)
    points = build_fixture(spec)
    report = run_replay(spec, points)
    output_dir.mkdir(parents=True, exist_ok=True)
    expanded_path = output_dir / "expanded_structural.jsonl"
    digest = write_expanded_fixture(points, expanded_path)
    report["expanded_fixture"] = {
        "path": str(expanded_path.resolve()),
        "sha256": digest,
        "point_count": len(points),
    }
    _write_json(output_dir / "structural_replay.json", report)
    return report


def build_blind_review_queue(
    assignments: Sequence[Mapping[str, Any]],
    samples: Sequence[Mapping[str, Any]],
    trace_records: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Select 3 stale, 3 stable, and 3 boundary treatment traces without labels."""

    samples_by_id = {str(sample["opaque_sample_id"]): sample for sample in samples}
    categories: dict[str, list[Mapping[str, Any]]] = {
        "stale": [],
        "stable": [],
        "boundary": [],
    }
    for assignment in assignments:
        if assignment["arm_name"] != "echo_off__freshness_render":
            continue
        sample = samples_by_id[str(assignment["opaque_sample_id"])]
        if sample["cohort"] == "boundary":
            category = "boundary"
        elif sample["stale_or_stable_reference"]["reference_state"] == "obsolete":
            category = "stale"
        else:
            category = "stable"
        categories[category].append(assignment)
    selected = [
        assignment
        for category in ("stale", "stable", "boundary")
        for assignment in sorted(
            categories[category],
            key=lambda item: str(item["opaque_sample_id"]),
        )[:3]
    ]
    if len(selected) != 9:
        raise ValueError("blind review queue requires 3 stale, 3 stable, and 3 boundary traces")
    queue: list[dict[str, Any]] = []
    for index, assignment in enumerate(selected, 1):
        sample = samples_by_id[str(assignment["opaque_sample_id"])]
        trace_record = trace_records[str(assignment["agent_input_sha256"])]
        queue.append(
            {
                "review_id": f"blind-{index:02d}",
                "sample_id": assignment["opaque_sample_id"],
                "user_prompt": sample["user_prompt"],
                "context_packet_text": assignment["context_packet_text"],
                "ordered_trace": trace_record["trace"],
                "manual_status": "pending",
            }
        )
    return queue


async def run_sentinel_phase(
    transport: StructuredModelTransport,
    sentinel_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    """Run the nine paid sentinel traces and freeze the hard-gate artifact."""

    sentinels = load_sentinels(sentinel_path)
    scorer = BehavioralScorer(transport, max_attempts=3)
    records = await asyncio.gather(*(scorer.score(sentinel, sentinel["trace"]) for sentinel in sentinels))
    comparisons = [
        {
            "sample_id": sentinel["opaque_sample_id"],
            "mismatches": sentinel_mismatches(record, sentinel),
        }
        for sentinel, record in zip(sentinels, records, strict=True)
    ]
    passed = all(
        record["call_status"] == "ok" and not comparison["mismatches"]
        for record, comparison in zip(records, comparisons, strict=True)
    )
    artifact = {
        "schema_version": "v0291-judge-smoke-artifact-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": MODEL_SNAPSHOT,
        "fixture_sha256": _sha256_bytes(sentinel_path.read_bytes()),
        "prompt_sha256": _sha256_text(JUDGE_SYSTEM_PROMPT),
        "schema_sha256": _sha256_object(JUDGE_SCHEMA),
        "passed": passed,
        "valid_count": sum(record["call_status"] == "ok" for record in records),
        "matched_count": sum(not comparison["mismatches"] for comparison in comparisons),
        "comparisons": comparisons,
        "records": records,
    }
    _write_json(output_path, artifact)
    return artifact


async def run_behavioral_phase(
    *,
    transport: StructuredModelTransport,
    behavior_manifest_path: Path,
    sentinel_artifact_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Generate, independently judge, and aggregate every unique blind input."""

    require_sentinel_gate(sentinel_artifact_path)
    manifest = load_behavioral_manifest(behavior_manifest_path)
    samples = expand_behavioral_samples(manifest)
    packet_assignments = materialize_behavioral_arms(manifest, samples)
    assignments, unique_inputs = prepare_agent_assignments(
        manifest,
        samples,
        packet_assignments,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(output_dir / "behavioral_assignments.jsonl", assignments)
    samples_by_id = {str(sample["opaque_sample_id"]): sample for sample in samples}
    representative_sample: dict[str, Mapping[str, Any]] = {}
    for assignment in assignments:
        representative_sample.setdefault(
            str(assignment["agent_input_sha256"]),
            samples_by_id[str(assignment["opaque_sample_id"])],
        )

    agent_path = output_dir / "agent_traces.jsonl"
    agent_records = load_unique_jsonl(
        agent_path,
        key_field="input_sha256",
        valid_status="ok",
    )
    generator = AgentTraceGenerator(transport)

    async def generate_one(digest: str, blind: Mapping[str, Any]) -> dict[str, Any]:
        sample = representative_sample[digest]
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                result = await generator.generate(blind, sample)
                record = {
                    "input_sha256": digest,
                    "call_status": "ok",
                    "attempts": attempt,
                    "sample_id": sample["opaque_sample_id"],
                    "trace": result.trace,
                    "call_records": result.call_records,
                    "error": None,
                }
                _append_jsonl(agent_path, record)
                return record
            except Exception as error:
                last_error = error
        assert last_error is not None
        record = {
            "input_sha256": digest,
            "call_status": "invalid",
            "attempts": 3,
            "sample_id": sample["opaque_sample_id"],
            "trace": None,
            "call_records": [],
            "error": str(last_error),
        }
        _append_jsonl(agent_path, record)
        return record

    missing_agent = {digest: blind for digest, blind in unique_inputs.items() if digest not in agent_records}
    generated = await asyncio.gather(*(generate_one(digest, blind) for digest, blind in missing_agent.items()))
    agent_records.update({record["input_sha256"]: record for record in generated if record["call_status"] == "ok"})
    if len(agent_records) != len(unique_inputs):
        raise GateBlocked(f"agent valid_count {len(agent_records)} does not equal expected_count {len(unique_inputs)}")

    judge_path = output_dir / "judge_records.jsonl"
    judge_records = load_unique_jsonl(
        judge_path,
        key_field="result_key",
        valid_status="ok",
    )
    scorer = BehavioralScorer(transport, max_attempts=3)

    async def judge_one(digest: str) -> dict[str, Any]:
        sample = representative_sample[digest]
        trace = agent_records[digest]["trace"]
        scored = await scorer.score(sample, trace)
        result_key = _judge_result_key(scored)
        record = {
            "result_key": result_key,
            "agent_input_sha256": digest,
            **scored,
        }
        _append_jsonl(judge_path, record)
        return record

    known_by_agent = _select_current_judge_records(judge_records, representative_sample, agent_records)
    judged = await asyncio.gather(*(judge_one(digest) for digest in unique_inputs if digest not in known_by_agent))
    known_by_agent.update({record["agent_input_sha256"]: record for record in judged if record["call_status"] == "ok"})
    if len(known_by_agent) != len(unique_inputs):
        raise GateBlocked(f"judge valid_count {len(known_by_agent)} does not equal expected_count {len(unique_inputs)}")

    scored_rows = [
        {
            **{
                key: assignment[key]
                for key in (
                    "opaque_sample_id",
                    "scenario_family_id",
                    "cohort",
                    "arm_name",
                    "applicable_dimensions",
                    "agent_input_sha256",
                )
            },
            "judge_output": known_by_agent[str(assignment["agent_input_sha256"])]["judge_output"],
        }
        for assignment in assignments
    ]
    _write_jsonl(output_dir / "scored_assignments.jsonl", scored_rows)
    aggregate = aggregate_behavioral_results(scored_rows)
    _write_json(output_dir / "behavioral_aggregate.json", aggregate)
    review_queue = build_blind_review_queue(assignments, samples, agent_records)
    _write_json(output_dir / "blind_review_queue.json", review_queue)
    return aggregate


def build_frozen_run_manifest(
    *,
    structure_fixture_path: Path,
    behavior_manifest_path: Path,
    sentinel_path: Path,
    behavior_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Freeze every code, prompt, model, tool, schema, and fixture identity."""

    repository_root = behavior_manifest_path.resolve().parents[2]
    code_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    return {
        "schema_version": "v0291-frozen-run-manifest-v1",
        "eval_manifest_version": behavior_manifest["eval_manifest_version"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "code_commit": code_commit,
        "model_snapshot": MODEL_SNAPSHOT,
        "fixture_sha256": {
            "structural": _sha256_bytes(structure_fixture_path.read_bytes()),
            "behavioral": _sha256_bytes(behavior_manifest_path.read_bytes()),
            "sentinel": _sha256_bytes(sentinel_path.read_bytes()),
        },
        "system_prompt_sha256": _sha256_object(
            {
                "agent_plan": AGENT_SYSTEM_PROMPT,
                "agent_final": AGENT_FINAL_SYSTEM_PROMPT,
            }
        ),
        "tool_contract_sha256": _sha256_object(behavior_manifest["tool_contract"]),
        "judge_prompt_sha256": _sha256_text(JUDGE_SYSTEM_PROMPT),
        "judge_schema_sha256": _sha256_object(JUDGE_SCHEMA),
    }


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(f"{_canonical_json(record)}\n" for record in records)
    path.write_text(content, encoding="utf-8")


def _append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"{_canonical_json(record)}\n")
        handle.flush()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _sha256_object(value: object) -> str:
    return _sha256_text(_canonical_json(value))
