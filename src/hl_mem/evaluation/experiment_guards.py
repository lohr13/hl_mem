"""Reusable fail-loud guards for frozen evaluation runs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_LEAK_KEYS = frozenset(
    {
        "gold",
        "answer",
        "answers",
        "answer_entities",
        "role_action_object",
        "forbidden_entities",
        "forbidden_assertions",
        "accepted_rubrics",
        "rubrics",
        "verdict",
    }
)


def assert_gold_free(payload: Any, *, path: str = "$") -> None:
    """Reject scorer fields before a payload reaches retrieval or a model."""
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            normalized = str(key).casefold()
            if (
                normalized in _LEAK_KEYS
                or any(token in normalized for token in ("gold", "forbidden", "rubric"))
                or normalized.endswith(("_gold", "_verdict", "_answer_ref"))
            ):
                raise ValueError(f"gold/scorer field forbidden at {path}.{key}")
            assert_gold_free(value, path=f"{path}.{key}")
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        for index, value in enumerate(payload):
            assert_gold_free(value, path=f"{path}[{index}]")


def assert_pilot_gate(preregistration_sha256: str, artifact: Mapping[str, Any]) -> None:
    """Require three completed pilot calls and at least one persisted result."""
    if artifact.get("preregistration_sha256") != preregistration_sha256:
        raise RuntimeError("pilot preregistration hash mismatch")
    calls = artifact.get("calls")
    if int(artifact.get("attempted", 0)) != 3 or not isinstance(calls, list) or len(calls) != 3:
        raise RuntimeError("pilot must contain exactly three completed calls")
    if int(artifact.get("accepted", 0)) <= 0 or int(artifact.get("persisted", 0)) <= 0:
        raise RuntimeError("pilot accepted/persisted count must be positive")


def bind_authoritative_source_context(
    raw: Mapping[str, Any],
    *,
    source_id: str,
    evidence: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Bind model-produced semantics to caller-owned claim and evidence IDs."""
    bound = dict(raw)
    bound["claim_id"] = source_id
    bound.pop("evidence_event_id", None)
    bound.pop("evidence_quote", None)
    bound.pop("_binding_reason", None)
    action = unicodedata.normalize("NFC", str(bound.get("action") or "")).strip()
    object_ = unicodedata.normalize("NFC", str(bound.get("object") or "")).strip()
    if not action or not object_:
        return bound
    for item in evidence:
        text = unicodedata.normalize("NFC", str(item.get("text") or ""))
        if action in text and object_ in text:
            bound["evidence_event_id"] = str(item["evidence_event_id"])
            bound["evidence_quote"] = text
            return bound
    bound["_binding_reason"] = "evidence_not_found"
    return bound


def _relation_counts(database: Path) -> tuple[int, int]:
    if not database.is_file():
        raise RuntimeError(f"relation cache is missing: {database}")
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = connection.execute("SELECT COUNT(*) FROM memory_relations").fetchone()
        total = int(row[0]) if row else 0
        relation_columns = {str(item[1]) for item in connection.execute("PRAGMA table_info(memory_relations)")}
        claim_columns = {str(item[1]) for item in connection.execute("PRAGMA table_info(claims)")}
        if {"from_id", "to_id"} <= relation_columns and {"id", "status"} <= claim_columns:
            row = connection.execute(
                "SELECT COUNT(*) FROM memory_relations AS relation "
                "JOIN claims AS source ON source.id=relation.from_id "
                "JOIN claims AS target ON target.id=relation.to_id "
                "WHERE source.status='active' AND target.status='active'"
            ).fetchone()
            recallable = int(row[0]) if row else 0
        else:
            recallable = total
    finally:
        connection.close()
    return total, recallable


def validate_relation_coverage(
    cases: Sequence[Mapping[str, Any]],
    databases: Mapping[str, Path],
) -> dict[str, Any]:
    """Require each materialized cache to match its relation declaration."""
    by_case: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    for case in cases:
        case_id = str(case["case_id"])
        declared = str(case.get("relation_coverage") or "")
        if declared not in {"required", "none"}:
            raise RuntimeError(f"relation coverage gate has invalid declaration: {case_id}={declared!r}")
        database = databases.get(case_id)
        if database is None:
            raise RuntimeError(f"relation coverage gate has no cache path: {case_id}")
        count, recallable = _relation_counts(database)
        by_case[case_id] = {
            "declared": declared,
            "relations": count,
            "recallable_relations": recallable,
        }
        if declared == "required" and recallable == 0:
            failures.append(f"{case_id}:{declared}={count},recallable=0")
        elif declared == "none" and count != 0:
            failures.append(f"{case_id}:{declared}={count}")
    if failures:
        raise RuntimeError(f"relation coverage gate failed: {', '.join(failures)}")
    ordered = dict(sorted(by_case.items()))
    return {
        "required_cases": sum(item["declared"] == "required" for item in ordered.values()),
        "required_with_edges": sum(
            item["declared"] == "required" and int(item["relations"]) > 0 for item in ordered.values()
        ),
        "required_with_recallable_edges": sum(
            item["declared"] == "required" and int(item["recallable_relations"]) > 0 for item in ordered.values()
        ),
        "none_cases": sum(item["declared"] == "none" for item in ordered.values()),
        "none_with_edges": sum(item["declared"] == "none" and int(item["relations"]) > 0 for item in ordered.values()),
        "total_relations": sum(int(item["relations"]) for item in ordered.values()),
        "total_recallable_relations": sum(int(item["recallable_relations"]) for item in ordered.values()),
        "by_case": ordered,
    }


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def assert_packet_variants_differ(
    packet_snapshot: Mapping[str, Any],
    required_case_ids: Sequence[str],
    preregistration_id: str,
    *,
    baseline_variant: str,
    candidate_variant: str,
) -> dict[str, Any]:
    """Prove a candidate path changes three frozen packets before a batch starts."""
    unique_ids = sorted(set(str(case_id) for case_id in required_case_ids))
    if len(unique_ids) < 3:
        raise RuntimeError("packet smoke requires at least 3 relation-required cases")
    sampled = sorted(
        unique_ids,
        key=lambda case_id: hashlib.sha256(f"{preregistration_id}{case_id}".encode()).hexdigest(),
    )[:3]
    packets = {
        (str(item["case_id"]), int(item["repeat_index"]), str(item["variant_id"])): item
        for item in packet_snapshot.get("packets") or []
    }
    equal_pairs: list[str] = []
    digests: dict[str, dict[str, str]] = {}
    for case_id in sampled:
        try:
            baseline = packets[(case_id, 0, baseline_variant)]["packet"]
            candidate = packets[(case_id, 0, candidate_variant)]["packet"]
        except KeyError as error:
            raise RuntimeError(f"packet smoke is missing a frozen pair: {case_id}") from error
        if baseline == candidate:
            equal_pairs.append(case_id)
        digests[case_id] = {
            baseline_variant: _canonical_hash(baseline),
            candidate_variant: _canonical_hash(candidate),
        }
    if equal_pairs:
        raise RuntimeError(f"packet smoke failed for equal packets: {', '.join(equal_pairs)}")
    return {"passed": True, "case_ids": sampled, "equal_pairs": [], "packet_sha256": digests}
