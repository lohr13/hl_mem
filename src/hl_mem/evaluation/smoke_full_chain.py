"""Zero-LLM smoke gate for the production state chain and scorer seam."""

from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from hl_mem.evaluation.runtime_guard import relaunch_evaluation_script

if __name__ == "__main__":
    _REPO_ROOT = Path(__file__).resolve().parents[3]
    _isolated_exit = relaunch_evaluation_script(Path(__file__), sys.argv[1:], _REPO_ROOT)
    if _isolated_exit is not None:
        raise SystemExit(_isolated_exit)

from hl_mem.application.ingest import IngestService
from hl_mem.domain.claims.state_projection import project_state_coordinate
from hl_mem.evaluation.state_experiment_scoring import load_persisted_edges, score_protocol
from hl_mem.evaluation.state_product_adapter import BoundProductEvidence, bind_product_evidence
from hl_mem.ingest.chunking import ChunkingPolicy
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.ingest.llm_extractor import LLM_EXTRACTOR_VERSION, LLMExtractor
from hl_mem.llm.client import LLMClient
from hl_mem.llm.types import LLMRequest, LLMResponse, StructuredOutputMode
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database


@dataclass(frozen=True)
class _Scenario:
    sample_id: str
    namespace: str
    subjects: tuple[str, ...]
    values: tuple[str, ...]
    events: tuple[str, ...]
    raw_claims: tuple[Mapping[str, Any], ...]
    expected_assertion_ids: tuple[str, ...]
    expected_source_indices: tuple[tuple[int, ...], ...]
    expected_edges: tuple[tuple[str, str], ...] = ()


class _FakeProvider:
    name = "fake"


class _ReplayClient:
    provider = _FakeProvider()
    model = "fake-state-smoke"

    def __init__(self, response: Mapping[str, Any]) -> None:
        self.response = dict(response)
        self.calls = 0

    def complete(self, request: LLMRequest, *, timeout_seconds: float | None = None) -> LLMResponse:
        del request, timeout_seconds
        self.calls += 1
        return LLMResponse(content=self.response, finish_reason="stop", usage_total_tokens=0)


def _raw_claim(
    subject: str,
    value: str,
    evidence: str,
    source_event_indices: Sequence[int] | None,
) -> dict[str, Any]:
    claim: dict[str, Any] = {
        "subject": subject,
        "value": value,
        "action": None,
        "object": None,
        "kind": "config",
        "confidence": 1.0,
        "notability": "high",
        "assertion_kind": "observation",
        "evidence_quote": evidence,
    }
    if source_event_indices is not None:
        claim["source_event_indices"] = list(source_event_indices)
    return claim


def _scenarios() -> tuple[_Scenario, ...]:
    upgrade_values = ("gateway current version is v1.0", "gateway current version is v2.0")
    default_value = "gateway current version is v3.0"
    composite_value = "gateway current version is v4.0"
    return (
        _Scenario(
            sample_id="smoke-upgrade",
            namespace="eval:smoke:upgrade",
            subjects=("gateway", "gateway"),
            values=upgrade_values,
            events=upgrade_values,
            raw_claims=(
                _raw_claim("gateway", upgrade_values[0], upgrade_values[0], (0,)),
                _raw_claim("gateway", upgrade_values[1], upgrade_values[1], (1,)),
            ),
            expected_assertion_ids=("smoke-upgrade:c0:a0", "smoke-upgrade:c1:a0"),
            expected_source_indices=((0,), (1,)),
            expected_edges=(("smoke-upgrade:c0:a0", "smoke-upgrade:c1:a0"),),
        ),
        _Scenario(
            sample_id="smoke-default-index",
            namespace="eval:smoke:default-index",
            subjects=("gateway",),
            values=(default_value,),
            events=(default_value,),
            raw_claims=(_raw_claim("gateway", default_value, default_value, None),),
            expected_assertion_ids=("smoke-default-index:c0:a0",),
            expected_source_indices=((0,),),
        ),
        _Scenario(
            sample_id="smoke-composite",
            namespace="eval:smoke:composite",
            subjects=("gateway",),
            values=(composite_value,),
            events=(composite_value, composite_value),
            raw_claims=(
                _raw_claim("gateway", composite_value, composite_value, (0,)),
                _raw_claim("gateway", composite_value, composite_value, (1,)),
            ),
            expected_assertion_ids=("smoke-composite:c0:a0",),
            expected_source_indices=((0, 1),),
        ),
    )


def _extract(scenario: _Scenario) -> tuple[list[ExtractedClaim], list[BoundProductEvidence]]:
    response: dict[str, Any] = {
        "claims": [dict(claim) for claim in scenario.raw_claims],
        "should_memorize": True,
    }
    client = _ReplayClient(response)
    extractor = LLMExtractor(
        cast(LLMClient, client),
        ChunkingPolicy(target_chars=12_000, overlap_turns=2, max_split_depth=2),
        schema_retries=0,
        structured_mode=StructuredOutputMode.JSON_OBJECT,
        verification_mode="off",
    )
    source_events = [
        {
            "id": f"{scenario.sample_id}:event:{index}",
            "actor_type": "user",
            "content": {"text": text},
            "occurred_at": f"2026-08-22T00:0{index}:00+00:00",
        }
        for index, text in enumerate(scenario.events)
    ]
    content = {
        "messages": [
            {
                "event_index": index,
                "speaker": "user",
                "turn": index,
                "occurred_at": source["occurred_at"],
                "content": text,
            }
            for index, (source, text) in enumerate(zip(source_events, scenario.events, strict=True))
        ]
    }
    products = extractor.extract(
        content,
        {
            "occurred_at": source_events[0]["occurred_at"],
            "actor_type": "conversation",
            "event_type": "message",
            "session_id": None,
            "recent_events": [],
            "_source_events": source_events,
        },
    )
    if client.calls != 1:
        raise RuntimeError(f"synthetic smoke expected one fake extraction call, got {client.calls}")
    bindings = bind_product_evidence(
        response["claims"],
        [asdict(product) for product in products],
        event_count=len(scenario.events),
        source_event_texts=list(scenario.events),
    )
    if len(products) != len(scenario.expected_assertion_ids) or len(bindings) != len(products):
        raise RuntimeError("synthetic product cardinality changed")
    return products, bindings


def _coordinate(namespace: str, subject: str) -> dict[str, Any]:
    return {
        "namespace": namespace,
        "canonical_subject": subject,
        "canonical_slot": "config.version",
        "coordinate_qualifiers": {},
    }


def _stored_projection(row: Mapping[str, Any], namespace: str) -> dict[str, Any]:
    coordinate = project_state_coordinate(
        namespace=namespace,
        subject=str(row.get("subject_entity_id") or ""),
        canonical_slot=str(row.get("canonical_slot")) if row.get("canonical_slot") is not None else None,
        qualifiers=dict(row.get("qualifiers") or {}),
    )
    thawed = None
    if coordinate is not None:
        thawed = {
            "namespace": coordinate.namespace,
            "canonical_subject": coordinate.canonical_subject,
            "canonical_slot": coordinate.canonical_slot,
            "coordinate_qualifiers": {key: json.loads(value) for key, value in coordinate.coordinate_qualifiers},
        }
    return {
        "coordinate": thawed,
        "canonical_subject": str(row.get("subject_entity_id") or ""),
        "canonical_slot": row.get("canonical_slot"),
        "coordinate_qualifiers": thawed["coordinate_qualifiers"] if thawed else {},
        "state_context": str((row.get("qualifiers") or {}).get("_state_context") or "current"),
        "reason_codes": ["production_smoke"],
    }


def _event(scenario: _Scenario, index: int) -> dict[str, Any]:
    return {
        "id": f"{scenario.sample_id}:event:{index}",
        "tenant_id": scenario.namespace,
        "actor_type": "user",
        "content": {"text": scenario.events[index]},
        "occurred_at": f"2026-08-22T00:0{index}:00+00:00",
        "extractor": "fake-state-smoke",
        "extractor_version": LLM_EXTRACTOR_VERSION,
    }


def _persist_scenario(
    connection: Any,
    repository: ClaimRepository,
    embedder: FakeEmbedder,
    scenario: _Scenario,
    products: Sequence[ExtractedClaim],
    bindings: Sequence[BoundProductEvidence],
    manifest: dict[str, str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    scored_claims: list[dict[str, Any]] = []
    gold_claims: list[dict[str, Any]] = []
    recorded_at = datetime.now(timezone.utc).isoformat()
    for index, (product, binding, assertion_id, expected_indices) in enumerate(
        zip(products, bindings, scenario.expected_assertion_ids, scenario.expected_source_indices, strict=True)
    ):
        indices = list(product.source_event_indices)
        primary = _event(scenario, indices[0])
        sources = [_event(scenario, source_index) for source_index in indices]
        stored = IngestService.store_extracted(
            connection,
            product,
            primary,
            recorded_at,
            embedder,
            source_events=sources,
        )
        if stored.claim_id is None:
            raise RuntimeError("synthetic production chain rejected a state claim")
        row = repository.get_claim(stored.claim_id)
        if row is None:
            raise RuntimeError("synthetic stored claim disappeared")
        projection = _stored_projection(row, scenario.namespace)
        raw = binding.raw_claim
        scored_claims.append(
            {
                "assertion_id": assertion_id,
                "source_claim_index": index,
                "atomic_index": 0,
                "atomicity": "atomic",
                "claim": {
                    "subject": product.subject,
                    "value": product.value,
                    "source_event_indices": indices,
                    "evidence_quote": str(raw.get("evidence_quote") or ""),
                },
                "projection": projection,
            }
        )
        manifest[str(stored.claim_id)] = assertion_id
        gold_claims.append(
            {
                "assertion_id": assertion_id,
                "coordinate": _coordinate(scenario.namespace, scenario.subjects[index]),
                "source_event_indices": list(expected_indices),
                "state_value": scenario.values[index],
            }
        )
    return (
        {
            "sample_id": scenario.sample_id,
            "arm": "production-smoke",
            "input_claim_count": len(scenario.raw_claims),
            "output_claim_count": len(scored_claims),
            "claims": scored_claims,
            "rejections": [],
        },
        gold_claims,
    )


def run_full_chain_smoke() -> dict[str, Any]:
    """Run synthetic production extraction through persisted scoring and return aggregates."""

    scenarios = _scenarios()
    with tempfile.TemporaryDirectory(prefix="hl-mem-state-smoke-") as temporary:
        database_path = Path(temporary) / "smoke.sqlite"
        database = Database(database_path)
        connection = database.open()
        repository = ClaimRepository(connection)
        embedder = FakeEmbedder(8)
        manifest: dict[str, str] = {}
        run: list[dict[str, Any]] = []
        gold: list[dict[str, Any]] = []
        corpus: list[dict[str, Any]] = []
        composite_binding = False
        default_source_index = False
        try:
            for scenario in scenarios:
                products, bindings = _extract(scenario)
                if scenario.sample_id == "smoke-default-index":
                    default_source_index = tuple(products[0].source_event_indices) == (0,) and (
                        "source_event_indices" not in scenario.raw_claims[0]
                    )
                if scenario.sample_id == "smoke-composite":
                    composite_binding = len(products) == 1 and len(bindings[0].raw_claims) == 2
                candidate_sample, gold_claims = _persist_scenario(
                    connection,
                    repository,
                    embedder,
                    scenario,
                    products,
                    bindings,
                    manifest,
                )
                run.append(candidate_sample)
                gold.append(
                    {
                        "sample_id": scenario.sample_id,
                        "atomic_claims": gold_claims,
                        "expected_supersede_edges": [list(edge) for edge in scenario.expected_edges],
                        "counterexample_zero_supersede": False,
                        "current_assertion_ids": [scenario.expected_assertion_ids[-1]],
                        "historical_assertion_ids": list(scenario.expected_assertion_ids),
                    }
                )
                corpus.append(
                    {
                        "bundle_id": scenario.sample_id,
                        "category": "synthetic_state_smoke",
                        "subtype": scenario.sample_id,
                        "events": [
                            {"event_index": index, "content": {"text": text}}
                            for index, text in enumerate(scenario.events)
                        ],
                    }
                )
            status_by_assertion = {
                manifest[str(row["id"])]: str(row["status"])
                for row in connection.execute("SELECT id,status FROM claims")
                if str(row["id"]) in manifest
            }
        finally:
            connection.close()

        persisted_snapshot = load_persisted_edges(database_path, manifest)
        persisted_edges = persisted_snapshot["edges"]
        all_assertion_ids = [assertion_id for scenario in scenarios for assertion_id in scenario.expected_assertion_ids]
        active_assertion_ids = [
            assertion_id for assertion_id in all_assertion_ids if status_by_assertion.get(assertion_id) == "active"
        ]
        report = score_protocol(
            gold,
            baseline_predictions={"claim_count": len(all_assertion_ids)},
            candidate_runs=[run, run, run],
            persisted_edges=persisted_edges,
            baseline_observations={
                "current_injected_assertion_ids": all_assertion_ids,
                "historical_retrieved_assertion_ids": all_assertion_ids,
            },
            candidate_observations={
                "current_injected_assertion_ids": active_assertion_ids,
                "historical_retrieved_assertion_ids": all_assertion_ids,
            },
            corpus_records=corpus,
        )

    admission_state_snapshot = all(
        claim["projection"]["canonical_slot"] == "config.version" for sample in run for claim in sample["claims"]
    )
    expected_edge = ("smoke-upgrade:c0:a0", "smoke-upgrade:c1:a0")
    resolver_supersede_edge = expected_edge in persisted_edges
    checks = dict(report["checks"])
    if len(checks) != 13:
        raise RuntimeError(f"state protocol emitted {len(checks)} checks, expected 13")
    seams = {
        "admission_state_snapshot": admission_state_snapshot,
        "default_source_index": default_source_index,
        "composite_binding": composite_binding,
        "resolver_supersede_edge": resolver_supersede_edge,
    }
    if not all(seams.values()):
        failed = ", ".join(name for name, passed in seams.items() if not passed)
        raise RuntimeError(f"state full-chain smoke seam failed: {failed}")
    return {
        "ok": True,
        "zero_llm": True,
        "seams": seams,
        "check_count": len(checks),
        "checks": checks,
        "protocol_passed": bool(report["passed"]),
        "threshold_satisfiable": bool(report["threshold_satisfiability"]["satisfiable"]),
        "counts": {
            name: {key: report["metrics"][name][key] for key in ("true_positive", "false_positive", "false_negative")}
            for name in ("atomic_claim", "state_coordinate", "supersede_edge")
        },
    }


def main() -> int:
    result = run_full_chain_smoke()
    summary = {
        "ok": result["ok"],
        "zero_llm": result["zero_llm"],
        "seams": result["seams"],
        "check_count": result["check_count"],
        "protocol_passed": result["protocol_passed"],
        "threshold_satisfiable": result["threshold_satisfiable"],
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
