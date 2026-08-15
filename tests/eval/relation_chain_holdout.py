"""Sealed relation-chain holdout manifest and explicit-access loader."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from tests.eval.chinese_e2e import AnswerEntityGold, SampleManifestError, _parse_answer_entity_gold

EXPECTED_CATEGORIES = {
    "recommendation_execution": 4,
    "reporting_ownership": 4,
    "enumeration_completeness": 4,
    "cross_event_two_hop": 4,
    "conflict_latest_value": 4,
    "no_answer_trap": 4,
}
ACCESS_POLICY = "sealed_final_preregistered_validation_only"


class SealedHoldoutError(ValueError):
    """Base error for the sealed relation-chain holdout."""


class SealedHoldoutAccessError(SealedHoldoutError):
    """The caller did not explicitly opt into sealed final-validation data."""


class HoldoutHashMismatch(SealedHoldoutError):
    """The installed payload differs from the frozen manifest."""


@dataclass(frozen=True)
class HoldoutManifest:
    schema_version: int
    dataset_id: str
    source_path: Path
    sha256: str
    case_count: int
    category_counts: dict[str, int]
    gold_schema_version: int
    scorer_version: str
    access_policy: str
    manifest_path: Path


@dataclass(frozen=True)
class HoldoutEvent:
    event_id: str
    occurred_at: str
    text: str


@dataclass(frozen=True)
class HoldoutCase:
    case_id: str
    category: str
    namespace: str
    events: tuple[HoldoutEvent, ...]
    question_at: str
    question: str
    answer: str
    gold: AnswerEntityGold
    provenance: str


@dataclass(frozen=True)
class HoldoutDataset:
    schema_version: int
    dataset_id: str
    cases: tuple[HoldoutCase, ...]


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SealedHoldoutError(f"{label} must be an object")
    return {str(key): item for key, item in value.items()}


def _list(value: object, label: str) -> list[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise SealedHoldoutError(f"{label} must be a list")
    return list(value)


def _timezone_timestamp(value: object, label: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise SealedHoldoutError(f"{label} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise SealedHoldoutError(f"{label} must include a timezone")
    return text


def load_holdout_manifest(path: Path) -> HoldoutManifest:
    raw = _mapping(json.loads(path.read_text(encoding="utf-8")), "manifest")
    expected_keys = {
        "schema_version",
        "dataset_id",
        "source_path",
        "sha256",
        "case_count",
        "category_counts",
        "gold_schema_version",
        "scorer_version",
        "access_policy",
    }
    if set(raw) != expected_keys:
        raise SealedHoldoutError(f"manifest must contain exactly {sorted(expected_keys)!r}")
    category_counts = {name: int(count) for name, count in _mapping(raw["category_counts"], "category_counts").items()}
    digest = str(raw["sha256"]).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise SealedHoldoutError("manifest sha256 must be a lowercase SHA-256 digest")
    manifest = HoldoutManifest(
        schema_version=int(raw["schema_version"]),
        dataset_id=str(raw["dataset_id"]),
        source_path=Path(str(raw["source_path"])),
        sha256=digest,
        case_count=int(raw["case_count"]),
        category_counts=category_counts,
        gold_schema_version=int(raw["gold_schema_version"]),
        scorer_version=str(raw["scorer_version"]),
        access_policy=str(raw["access_policy"]),
        manifest_path=path.resolve(),
    )
    if (
        manifest.schema_version != 1
        or manifest.dataset_id != "zh-relation-chain-holdout-v1"
        or manifest.case_count != 24
        or manifest.category_counts != EXPECTED_CATEGORIES
        or manifest.gold_schema_version != 3
        or manifest.scorer_version != "answer-entity-packet-v1"
        or manifest.access_policy != ACCESS_POLICY
    ):
        raise SealedHoldoutError("sealed holdout manifest does not match the frozen v1 contract")
    return manifest


def resolve_holdout_path(manifest: HoldoutManifest) -> Path:
    expanded = manifest.source_path.expanduser()
    if expanded.is_absolute():
        return expanded
    return manifest.manifest_path.parent / expanded


def _parse_event(raw_event: object, label: str) -> HoldoutEvent:
    event = _mapping(raw_event, label)
    if set(event) != {"event_id", "occurred_at", "text"}:
        raise SealedHoldoutError(f"{label} must contain event_id, occurred_at, and text")
    event_id = str(event["event_id"]).strip()
    text = str(event["text"]).strip()
    if not event_id or not text:
        raise SealedHoldoutError(f"{label} event_id and text must not be empty")
    return HoldoutEvent(
        event_id=event_id,
        occurred_at=_timezone_timestamp(event["occurred_at"], f"{label}.occurred_at"),
        text=text,
    )


def _parse_case(raw_case: object, index: int) -> HoldoutCase:
    label = f"cases[{index}]"
    case = _mapping(raw_case, label)
    expected_keys = {
        "case_id",
        "category",
        "namespace",
        "events",
        "question_at",
        "question",
        "answer",
        "gold",
        "provenance",
    }
    if set(case) != expected_keys:
        raise SealedHoldoutError(f"{label} must contain exactly {sorted(expected_keys)!r}")
    case_id = str(case["case_id"]).strip()
    expected_id = f"rc-holdout-v1-{index + 1:03d}"
    if case_id != expected_id:
        raise SealedHoldoutError(f"{label}.case_id must be {expected_id!r}")
    category = str(case["category"]).strip()
    if category not in EXPECTED_CATEGORIES:
        raise SealedHoldoutError(f"{label}.category is not frozen")
    events = tuple(
        _parse_event(item, f"{label}.events[{event_index}]")
        for event_index, item in enumerate(_list(case["events"], f"{label}.events"))
    )
    if not events or len({event.event_id for event in events}) != len(events):
        raise SealedHoldoutError(f"{label}.events must be non-empty with unique event IDs")
    namespace = str(case["namespace"]).strip()
    question = str(case["question"]).strip()
    answer = str(case["answer"]).strip()
    provenance = str(case["provenance"]).strip()
    if not namespace or not question or not answer or not provenance:
        raise SealedHoldoutError(f"{label} text fields must not be empty")
    try:
        gold = _parse_answer_entity_gold(
            {case_id: case["gold"]},
            schema_version=3,
            expected_case_ids={case_id},
        )[case_id]
    except SampleManifestError as error:
        raise SealedHoldoutError(f"{label}.gold: {error}") from error
    return HoldoutCase(
        case_id=case_id,
        category=category,
        namespace=namespace,
        events=events,
        question_at=_timezone_timestamp(case["question_at"], f"{label}.question_at"),
        question=question,
        answer=answer,
        gold=gold,
        provenance=provenance,
    )


def load_sealed_holdout(path: Path, *, allow_sealed: bool = False) -> HoldoutDataset:
    if not allow_sealed:
        raise SealedHoldoutAccessError(
            "sealed holdout access requires allow_sealed=True and is reserved for final preregistered validation"
        )
    manifest = load_holdout_manifest(path)
    payload_path = resolve_holdout_path(manifest)
    payload = payload_path.read_bytes()
    actual_hash = hashlib.sha256(payload).hexdigest()
    if actual_hash != manifest.sha256:
        raise HoldoutHashMismatch(f"sealed holdout SHA-256 mismatch: expected {manifest.sha256}, got {actual_hash}")
    raw = _mapping(json.loads(payload.decode("utf-8")), "holdout")
    if set(raw) != {"schema_version", "dataset_id", "cases"}:
        raise SealedHoldoutError("holdout payload must contain schema_version, dataset_id, and cases")
    if int(raw["schema_version"]) != 1 or str(raw["dataset_id"]) != manifest.dataset_id:
        raise SealedHoldoutError("holdout payload identity does not match the manifest")
    cases = tuple(_parse_case(item, index) for index, item in enumerate(_list(raw["cases"], "cases")))
    if len(cases) != manifest.case_count:
        raise SealedHoldoutError("holdout case count does not match the manifest")
    counts = Counter(case.category for case in cases)
    if dict(counts) != manifest.category_counts:
        raise SealedHoldoutError("holdout category distribution does not match the manifest")
    return HoldoutDataset(schema_version=1, dataset_id=manifest.dataset_id, cases=cases)
