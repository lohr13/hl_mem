"""Run the frozen, synthetic exact-entity recall protocol."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import re
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from statistics import mean
from typing import Any, cast

import hl_mem
from hl_mem.application.recall import RecallService
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.storage.entities import EntityRepository

PROTOCOL_ID = "hl-mem-entity-v1"
DEFAULT_PROTOCOL = Path(__file__).with_name("entity_v1_protocol.json")
STABLE_ID = re.compile(r"^[a-z0-9][a-z0-9:._-]*$")
PHASE_1_COMMIT = "1f7e5cc23875dccb4503979c8a05733b8f069e97"
NOW = "2026-08-31T12:00:00+00:00"
VALID_FROM = "2026-01-01T00:00:00+00:00"
VALID_TO = "2026-06-01T00:00:00+00:00"


def load_protocol(path: Path = DEFAULT_PROTOCOL) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("entity protocol must be a JSON object")
    return payload


def protocol_hash(protocol: dict[str, Any]) -> str:
    frozen = {key: value for key, value in protocol.items() if key != "fixture_sha256"}
    cases = frozen.get("cases")
    if isinstance(cases, list):
        frozen["cases"] = [
            (
                {key: value for key, value in case.items() if not key.startswith("actual_") and key != "result"}
                if isinstance(case, dict)
                else case
            )
            for case in cases
        ]
    canonical = json.dumps(frozen, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_protocol(protocol: dict[str, Any]) -> None:
    if protocol.get("protocol_id") != PROTOCOL_ID or protocol.get("schema_version") != 1:
        raise ValueError("entity protocol identity or schema version is invalid")
    cases = protocol.get("cases")
    if not isinstance(cases, list) or len(cases) != 24:
        raise ValueError("entity protocol must contain exactly 24 cases")
    seen: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("entity protocol cases must be objects")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not STABLE_ID.fullmatch(case_id) or case_id in seen:
            raise ValueError("entity protocol case IDs must be unique and stable")
        seen.add(case_id)
        if case.get("synthetic") is not True or not isinstance(case.get("query"), str):
            raise ValueError(f"{case_id}: case must contain a synthetic query")
        if case.get("expected_scope") not in {"entity", "wide"}:
            raise ValueError(f"{case_id}: expected_scope must be entity or wide")
        if not isinstance(case.get("wide_equivalent"), bool):
            raise ValueError(f"{case_id}: wide_equivalent must be boolean")
        for field in ("expected_claim_ids", "forbidden_entity_ids"):
            values = case.get(field)
            if not isinstance(values, list) or not all(
                isinstance(value, str) and STABLE_ID.fullmatch(value) for value in values
            ):
                raise ValueError(f"{case_id}: {field} must contain stable IDs")
    if protocol_hash(protocol) != protocol.get("fixture_sha256"):
        raise ValueError("entity protocol fixture hash does not match")


class RecordingFakeEmbedder:
    """Count deterministic query embeddings without retaining them in evidence."""

    model = "fake"

    def __init__(self, dim: int) -> None:
        self.dim = dim
        self._delegate = FakeEmbedder(dim)
        self.call_count = 0

    def embed_one(self, text: str) -> bytes:
        self.call_count += 1
        return cast(bytes, self._delegate.embed_one(text))

    def embed_batch(self, texts: list[str]) -> list[bytes]:
        return [self.embed_one(text) for text in texts]


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _git_commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    commit = completed.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("git returned an invalid commit ID")
    return commit


def _insert_event(connection: sqlite3.Connection, event_id: str, namespace: str) -> None:
    connection.execute(
        "INSERT INTO events(id,tenant_id,event_type,actor_type,content_json,occurred_at,recorded_at) "
        "VALUES (?,?,'message','user','{}',?,?)",
        (event_id, namespace, VALID_FROM, VALID_FROM),
    )


def _insert_proof(connection: sqlite3.Connection, proof_id: str, claim_id: str, event_id: str) -> None:
    connection.execute(
        "INSERT INTO evidence_links(id,derived_type,derived_id,evidence_type,evidence_id,relation) "
        "VALUES (?,'claim',?,'event',?,'supports')",
        (proof_id, claim_id, event_id),
    )


def _create_entity(
    repository: EntityRepository,
    entity_id: str,
    display_name: str,
    aliases: tuple[str, ...],
    namespace: str,
) -> dict[str, dict[str, Any]]:
    entity_type, canonical_key = entity_id.split(":", maxsplit=1)
    repository.create_entity(
        entity_id,
        entity_type,
        canonical_key,
        display_name,
        namespace_key=namespace,
        now=VALID_FROM,
    )
    return {
        alias: repository.create_alias(
            alias,
            entity_type,
            entity_id,
            "user_explicit",
            namespace_key=namespace,
            valid_from=VALID_FROM,
        )
        for alias in aliases
    }


def _insert_claim(
    connection: sqlite3.Connection,
    embedder: FakeEmbedder,
    *,
    claim_id: str,
    namespace: str,
    text: str,
    subject_entity_id: str,
    subject_canonical_entity_id: str | None = None,
    canonical_target_entity_id: str | None = None,
    status: str = "active",
    valid_from: str = VALID_FROM,
    valid_to: str | None = None,
    recorded_from: str = VALID_FROM,
) -> None:
    ClaimRepository(connection).insert_claim(
        {
            "id": claim_id,
            "namespace_key": namespace,
            "subject_entity_id": subject_entity_id,
            "subject_canonical_entity_id": subject_canonical_entity_id,
            "canonical_target_entity_id": canonical_target_entity_id,
            "predicate": "state",
            "value": text,
            "index_text": text,
            "canonical_attribute": "state.service",
            "assertion_kind": "observation",
            "scope": "permanent",
            "status": status,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "recorded_from": recorded_from,
            "confidence": 0.95,
            "importance": 0.7,
            "embedding_dense": embedder.embed_one(text),
        }
    )


def _link_claim(
    connection: sqlite3.Connection,
    entities: EntityRepository,
    *,
    claim_id: str,
    entity_id: str,
    role: str,
    alias: str,
    alias_row: dict[str, Any],
    namespace: str,
) -> None:
    event_id = f"event:{claim_id}"
    proof_id = f"proof:{claim_id}:{role}"
    _insert_event(connection, event_id, namespace)
    _insert_proof(connection, proof_id, claim_id, event_id)
    entities.link_claim(
        claim_id,
        entity_id,
        role,
        mention_text=alias,
        alias_version=int(alias_row["version"]),
        proof_id=proof_id,
    )
    connection.commit()


def _seed_decoys(
    case: dict[str, Any],
    connection: sqlite3.Connection,
    entities: EntityRepository,
    embedder: FakeEmbedder,
) -> None:
    namespace = str(case["namespace"])
    forbidden = case["forbidden_entity_ids"]
    decoy_entity_id = str(forbidden[0]) if forbidden else "agent:noise"
    _create_entity(entities, decoy_entity_id, "Synthetic Noise", (), namespace)
    for index in range(30):
        _insert_claim(
            connection,
            embedder,
            claim_id=f"claim:{case['id']}:decoy-{index:02d}",
            namespace=namespace,
            text=str(case["query"]),
            subject_entity_id=decoy_entity_id,
            subject_canonical_entity_id=decoy_entity_id,
            recorded_from=f"2026-01-01T00:{index:02d}:00+00:00",
        )


def _seed_target_claim(
    case: dict[str, Any],
    connection: sqlite3.Connection,
    entities: EntityRepository,
    embedder: FakeEmbedder,
    *,
    entity_id: str | None,
    alias: str | None = None,
    alias_row: dict[str, Any] | None = None,
    role: str = "subject",
    status: str = "active",
    valid_to: str | None = None,
) -> str:
    claim_id = str(case["expected_claim_ids"][0])
    namespace = str(case["namespace"])
    _insert_claim(
        connection,
        embedder,
        claim_id=claim_id,
        namespace=namespace,
        text=str(case["query"]),
        subject_entity_id=entity_id or "synthetic:unscoped",
        subject_canonical_entity_id=entity_id if role == "subject" else None,
        canonical_target_entity_id=entity_id if role == "target" else None,
        status=status,
        valid_to=valid_to,
        recorded_from="2026-01-02T00:00:00+00:00",
    )
    if entity_id is not None and alias is not None and alias_row is not None:
        _link_claim(
            connection,
            entities,
            claim_id=claim_id,
            entity_id=entity_id,
            role=role,
            alias=alias,
            alias_row=alias_row,
            namespace=namespace,
        )
    return claim_id


def _seed_fixture(case: dict[str, Any], connection: sqlite3.Connection, embedder: FakeEmbedder) -> None:
    namespace = str(case["namespace"])
    fixture = str(case["fixture"])
    entities = EntityRepository(connection)
    _seed_decoys(case, connection, entities, embedder)

    if fixture == "no_entity":
        _seed_target_claim(case, connection, entities, embedder, entity_id=None)
        return

    if fixture in {"orion_subject", "orion_temporal", "orion_namespace", "orion_storage_failure"}:
        aliases = _create_entity(entities, "agent:orion", "Orion Agent", ("Orion", "猎户"), namespace)
        target_alias = "猎户" if "猎户" in str(case["query"]) else "Orion"
        if fixture == "orion_temporal" and case["intent"] == "historical":
            _seed_target_claim(
                case,
                connection,
                entities,
                embedder,
                entity_id="agent:orion",
                alias=target_alias,
                alias_row=aliases[target_alias],
                status="superseded",
                valid_to=VALID_TO,
            )
            _insert_claim(
                connection,
                embedder,
                claim_id=f"claim:{case['id']}:current",
                namespace=namespace,
                text=str(case["query"]),
                subject_entity_id="agent:orion",
                subject_canonical_entity_id="agent:orion",
                recorded_from="2026-07-01T00:00:00+00:00",
            )
            _link_claim(
                connection,
                entities,
                claim_id=f"claim:{case['id']}:current",
                entity_id="agent:orion",
                role="subject",
                alias=target_alias,
                alias_row=aliases[target_alias],
                namespace=namespace,
            )
        else:
            _seed_target_claim(
                case,
                connection,
                entities,
                embedder,
                entity_id="agent:orion",
                alias=target_alias,
                alias_row=aliases[target_alias],
            )
        return

    if fixture == "historical_alias":
        aliases = _create_entity(entities, "agent:orion", "Orion Agent", ("Legacy Orion",), namespace)
        _seed_target_claim(
            case,
            connection,
            entities,
            embedder,
            entity_id="agent:orion",
            alias="Legacy Orion",
            alias_row=aliases["Legacy Orion"],
        )
        entities.close_alias("Legacy Orion", "agent", namespace_key=namespace, valid_to=VALID_TO)
        connection.commit()
        return

    if fixture in {"atlas_cross_type", "nimbus_same_span"}:
        alias = "Atlas" if fixture == "atlas_cross_type" else "Nimbus"
        target_id = "agent:atlas" if fixture == "atlas_cross_type" else "device:nimbus"
        other_id = "project:atlas" if fixture == "atlas_cross_type" else "environment:nimbus"
        target_aliases = _create_entity(entities, target_id, f"{alias} Primary", (alias,), namespace)
        other_aliases = _create_entity(entities, other_id, f"{alias} Secondary", (alias,), namespace)
        _seed_target_claim(
            case,
            connection,
            entities,
            embedder,
            entity_id=target_id,
            alias=alias,
            alias_row=target_aliases[alias],
        )
        other_claim = f"claim:{case['id']}:ambiguous"
        _insert_claim(
            connection,
            embedder,
            claim_id=other_claim,
            namespace=namespace,
            text=str(case["query"]),
            subject_entity_id=other_id,
            subject_canonical_entity_id=other_id,
        )
        _link_claim(
            connection,
            entities,
            claim_id=other_claim,
            entity_id=other_id,
            role="subject",
            alias=alias,
            alias_row=other_aliases[alias],
            namespace=namespace,
        )
        return

    if fixture == "phoenix_overlap":
        long_aliases = _create_entity(
            entities,
            "project:project_phoenix",
            "Project Phoenix",
            ("Project Phoenix",),
            namespace,
        )
        short_aliases = _create_entity(entities, "agent:phoenix", "Phoenix Agent", ("Phoenix",), namespace)
        _seed_target_claim(
            case,
            connection,
            entities,
            embedder,
            entity_id="project:project_phoenix",
            alias="Project Phoenix",
            alias_row=long_aliases["Project Phoenix"],
        )
        other_claim = f"claim:{case['id']}:overlap"
        _insert_claim(
            connection,
            embedder,
            claim_id=other_claim,
            namespace=namespace,
            text=str(case["query"]),
            subject_entity_id="agent:phoenix",
            subject_canonical_entity_id="agent:phoenix",
        )
        _link_claim(
            connection,
            entities,
            claim_id=other_claim,
            entity_id="agent:phoenix",
            role="subject",
            alias="Phoenix",
            alias_row=short_aliases["Phoenix"],
            namespace=namespace,
        )
        return

    if fixture == "orion_and_phoenix":
        orion = _create_entity(entities, "agent:orion", "Orion Agent", ("Orion",), namespace)
        phoenix = _create_entity(entities, "project:phoenix", "Phoenix Project", ("Phoenix",), namespace)
        _seed_target_claim(
            case,
            connection,
            entities,
            embedder,
            entity_id="agent:orion",
            alias="Orion",
            alias_row=orion["Orion"],
        )
        other_claim = f"claim:{case['id']}:phoenix"
        _insert_claim(
            connection,
            embedder,
            claim_id=other_claim,
            namespace=namespace,
            text=str(case["query"]),
            subject_entity_id="project:phoenix",
            subject_canonical_entity_id="project:phoenix",
        )
        _link_claim(
            connection,
            entities,
            claim_id=other_claim,
            entity_id="project:phoenix",
            role="subject",
            alias="Phoenix",
            alias_row=phoenix["Phoenix"],
            namespace=namespace,
        )
        return

    if fixture == "cedar_incomplete":
        aliases = _create_entity(entities, "project:cedar", "Cedar Project", ("Cedar",), namespace)
        _seed_target_claim(
            case,
            connection,
            entities,
            embedder,
            entity_id="project:cedar",
            alias="Cedar",
            alias_row=aliases["Cedar"],
        )
        _insert_claim(
            connection,
            embedder,
            claim_id=f"claim:{case['id']}:unlinked",
            namespace=namespace,
            text=str(case["query"]),
            subject_entity_id="project:cedar",
            subject_canonical_entity_id="project:cedar",
        )
        return

    fixture_entities = {
        "phoenix_project": ("project:phoenix", "Phoenix Project", "Phoenix", "subject"),
        "mira_person": ("person:mira", "Mira Person", "Mira", "subject"),
        "node_seven_device": ("device:node_seven", "Node Seven", "NodeSeven", "subject"),
        "node_seven_target": ("device:node_seven", "Node Seven", "NodeSeven", "target"),
        "cedar_project": ("project:cedar", "Cedar Project", "青杉", "subject"),
        "inactive_entity": ("agent:dormant", "Dormant Agent", "Dormant", "subject"),
    }
    if fixture not in fixture_entities:
        raise ValueError(f"unknown entity fixture: {fixture}")
    entity_id, display_name, alias, role = fixture_entities[fixture]
    aliases = _create_entity(entities, entity_id, display_name, (alias,), namespace)
    _seed_target_claim(
        case,
        connection,
        entities,
        embedder,
        entity_id=entity_id,
        alias=alias,
        alias_row=aliases[alias],
        role=role,
    )
    if fixture == "inactive_entity":
        connection.execute(
            "UPDATE canonical_entities SET status='retired',updated_at=? WHERE namespace_key=? AND id=?",
            (NOW, namespace, entity_id),
        )
        connection.commit()


def _case_scope(trace: dict[str, Any]) -> str:
    mode = str(trace.get("entity_filter_mode") or "off")
    return "entity" if mode in {"entity", "enforce"} else ("observe" if mode == "observe" else "wide")


def _run_case(case: dict[str, Any], base_settings: Settings, mode: str, root: Path) -> dict[str, Any]:
    database = Database(root / f"{case['id']}.db")
    connection = database.open()
    dim = 32
    seed_embedder = FakeEmbedder(dim)
    query_embedder = RecordingFakeEmbedder(dim)
    try:
        _seed_fixture(case, connection, seed_embedder)
        settings = replace(
            base_settings,
            database_path=str(root / f"{case['id']}.db"),
            embedding_dim=dim,
            entity_constraint_mode=mode,
            recall_candidate_floor=5,
            recall_vector_scan_limit=25,
            reranker_mode="off",
            query_expansion_mode="off",
            relation_expansion_mode="off",
            tag_boost_enabled=False,
            resurrection_mode="off",
            freshness_annotation_mode="off",
            recall_dedup_threshold=0.0,
            echo_suppression_mode="off",
        )
        if case["category"] == "storage_failure":
            connection.set_authorizer(
                lambda action, table, _column, _database, _source: (
                    sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_READ and table == "entity_aliases"
                    else sqlite3.SQLITE_OK
                )
            )
        started = time.perf_counter()
        response = RecallService(connection, query_embedder, settings=settings).recall(
            str(case["query"]),
            limit=5,
            intent=str(case["intent"]),
            namespace=str(case["namespace"]),
            debug=True,
            ranking_now=NOW,
        )
        latency_ms = (time.perf_counter() - started) * 1000.0
        connection.set_authorizer(None)
        raw_results = response.get("results")
        results = raw_results if isinstance(raw_results, list) else []
        actual_ids = [str(item["id"]) for item in results if isinstance(item, dict) and "id" in item]
        trace_value = response.get("search_trace")
        trace = trace_value if isinstance(trace_value, dict) else {}
        top_entities: list[str] = []
        for claim_id in actual_ids:
            row = connection.execute(
                "SELECT subject_canonical_entity_id,canonical_target_entity_id FROM claims WHERE id=?",
                (claim_id,),
            ).fetchone()
            if row is not None:
                top_entities.extend(str(value) for value in row if value is not None)
        expected_ids = [str(value) for value in case["expected_claim_ids"]]
        forbidden = {str(value) for value in case["forbidden_entity_ids"]}
        channel_counts = {"fts": 0, "dense": 0}
        for candidate in (trace.get("candidates") or {}).values():
            if not isinstance(candidate, dict):
                continue
            for channel in channel_counts:
                if channel in (candidate.get("channels") or {}):
                    channel_counts[channel] += 1
        return {
            "id": str(case["id"]),
            "category": str(case["category"]),
            "expected_scope": str(case["expected_scope"]),
            "actual_scope": _case_scope(trace),
            "scope_signal": str(trace.get("entity_filter_mode") or "off"),
            "fallback_reason": trace.get("entity_fallback_reason"),
            "confidence_class": str(
                (trace.get("entity_resolution") or {}).get("confidence_class", "low")
                if isinstance(trace.get("entity_resolution"), dict)
                else "low"
            ),
            "expected_claim_ids": expected_ids,
            "actual_claim_ids": actual_ids,
            "hit_at_1": bool(actual_ids and actual_ids[0] in expected_ids),
            "hit_at_5": any(claim_id in expected_ids for claim_id in actual_ids[:5]),
            "forbidden_top_1": bool(top_entities and top_entities[0] in forbidden),
            "forbidden_entity_hits": sum(entity_id in forbidden for entity_id in top_entities),
            "wide_equivalent": bool(case["wide_equivalent"]),
            "embedding_calls": query_embedder.call_count,
            "llm_calls": 0,
            "reranker_calls": 0,
            "channel_counts": channel_counts,
            "latency_ms": latency_ms,
        }
    finally:
        connection.set_authorizer(None)
        connection.close()


def run_entity_protocol(settings: Settings, *, output: Path, mode: str) -> dict[str, Any]:
    """Run isolated deterministic cases and write content-free regression evidence."""

    if mode not in {"off", "observe", "enforce"}:
        raise ValueError("entity protocol mode must be off, observe, or enforce")
    protocol = load_protocol()
    validate_protocol(protocol)
    with tempfile.TemporaryDirectory(prefix="hl-mem-entity-v1-") as temporary_directory:
        root = Path(temporary_directory)
        records = [_run_case(case, settings, mode, root) for case in protocol["cases"]]
    latencies = [float(record["latency_ms"]) for record in records]
    result: dict[str, Any] = {
        "schema_version": 1,
        "protocol_id": PROTOCOL_ID,
        "fixture_sha256": protocol["fixture_sha256"],
        "commit": _git_commit(),
        "package_version": hl_mem.__version__,
        "python_version": platform.python_version(),
        "mode": mode,
        "case_count": len(records),
        "metrics": {
            "hit_at_1": mean(float(record["hit_at_1"]) for record in records),
            "hit_at_5": mean(float(record["hit_at_5"]) for record in records),
            "cross_entity_top_1_count": sum(bool(record["forbidden_top_1"]) for record in records),
        },
        "latency_ms": {"p50": _percentile(latencies, 0.50), "p95": _percentile(latencies, 0.95)},
        "embedding_calls": sum(int(record["embedding_calls"]) for record in records),
        "llm_calls": 0,
        "reranker_calls": 0,
        "external_model_calls": 0,
        "cases": records,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("off", "observe", "enforce"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        result = run_entity_protocol(Settings.for_test(), output=arguments.output, mode=arguments.mode)
    except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error) as error:
        print(f"Exact-entity protocol failed: {error}")
        return 1
    print(
        "Exact-entity protocol passed | "
        f"cases={result['case_count']} hit@5={result['metrics']['hit_at_5']:.4f} "
        f"p95={result['latency_ms']['p95']:.1f}ms"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
