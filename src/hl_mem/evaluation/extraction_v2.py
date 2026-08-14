"""Strict extraction-gold v2 contract and deterministic structure metrics.

The public fixture for this module is synthetic. Private evaluation corpora can use
the same schema from outside the repository (for example ``~/hl_mem_eval_data``).
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Literal


class ExtractionGoldError(ValueError):
    """Extraction-v2 gold data does not satisfy the frozen contract."""


@dataclass(frozen=True)
class GoldEvent:
    speaker: str
    text: str


@dataclass(frozen=True)
class RoleActionObjectAnchors:
    roles: tuple[str, ...]
    actions: tuple[str, ...]
    objects: tuple[str, ...]
    ordered_anchors: tuple[str, ...]


@dataclass(frozen=True)
class GoldModality:
    asserted: Literal["recommended", "planned", "considered", "executed", "completed", "owned"]
    positive_anchors: tuple[str, ...]
    forbidden_assertions: tuple[str, ...]


@dataclass(frozen=True)
class GoldAtomicFactUnit:
    unit_id: str
    source_event_indices: tuple[int, ...]
    role_action_object: RoleActionObjectAnchors
    proper_entities: tuple[str, ...]
    speaker: str
    canonical_subject: str
    forbidden_propagation: tuple[str, ...]
    modality: GoldModality | None
    requires_self_contained_chain: bool


@dataclass(frozen=True)
class ExtractionGoldCase:
    case_id: str
    experiment: Literal["entities_hybrid_a", "proper_noun_prompt_b"]
    category: str
    events: tuple[GoldEvent, ...]
    gold_units: tuple[GoldAtomicFactUnit, ...]


@dataclass(frozen=True)
class DedupExample:
    subject: str
    value: str
    proper_entities: tuple[str, ...]


@dataclass(frozen=True)
class DedupGoldPair:
    pair_id: str
    left: DedupExample
    right: DedupExample
    expected: Literal["reuse", "distinct"]


@dataclass(frozen=True)
class ExtractionGoldCorpus:
    schema_version: int
    data_classification: str
    cases: tuple[ExtractionGoldCase, ...]
    dedup_pairs: tuple[DedupGoldPair, ...]


@dataclass(frozen=True)
class ExtractionCaseScore:
    relation_role_direction: float
    modality_negative: float
    entity_precision: float
    entity_recall: float
    entity_f1: float
    forbidden_propagation: float
    canonical_subject: float
    chain_atomicity: float


@dataclass(frozen=True)
class ExtractionMajorityScore:
    sample_count: int
    majority: dict[str, float]
    support: dict[str, float]


@dataclass(frozen=True)
class DedupPairScore:
    total: int
    correct: int
    false_reuse_count: int
    reuse_precision: float
    reuse_recall: float
    distinct_recall: float
    accuracy: float


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ExtractionGoldError(f"{label} must be an object")
    return {str(key): item for key, item in value.items()}


def _list(value: object, label: str) -> list[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ExtractionGoldError(f"{label} must be a list")
    return list(value)


def _exact_keys(value: Mapping[str, Any], required: set[str], label: str) -> None:
    missing = required - set(value)
    extra = set(value) - required
    if missing:
        raise ExtractionGoldError(f"{label} missing {sorted(missing)[0]}")
    if extra:
        raise ExtractionGoldError(f"{label} has unknown field {sorted(extra)[0]}")


def _text(value: object, label: str) -> str:
    result = value.strip() if isinstance(value, str) else ""
    if not result:
        raise ExtractionGoldError(f"{label} must be a non-empty string")
    return result


def _unique_texts(value: object, label: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    values = tuple(_text(item, f"{label}[]") for item in _list(value, label))
    if not allow_empty and not values:
        raise ExtractionGoldError(f"{label} must not be empty")
    if len(values) != len(set(values)):
        raise ExtractionGoldError(f"{label} must not contain duplicates")
    return values


def _parse_relation(value: object, label: str) -> RoleActionObjectAnchors:
    raw = _mapping(value, label)
    _exact_keys(raw, {"roles", "actions", "objects", "ordered_anchors"}, label)
    relation = RoleActionObjectAnchors(
        roles=_unique_texts(raw["roles"], f"{label}.roles"),
        actions=_unique_texts(raw["actions"], f"{label}.actions"),
        objects=_unique_texts(raw["objects"], f"{label}.objects"),
        ordered_anchors=_unique_texts(raw["ordered_anchors"], f"{label}.ordered_anchors"),
    )
    missing = set(relation.roles + relation.actions + relation.objects) - set(relation.ordered_anchors)
    if missing:
        raise ExtractionGoldError(f"{label}.ordered_anchors must include {sorted(missing)[0]!r}")
    return relation


def _parse_modality(value: object, label: str) -> GoldModality | None:
    if value is None:
        return None
    raw = _mapping(value, label)
    _exact_keys(raw, {"asserted", "positive_anchors", "forbidden_assertions"}, label)
    asserted = _text(raw["asserted"], f"{label}.asserted")
    allowed = {"recommended", "planned", "considered", "executed", "completed", "owned"}
    if asserted not in allowed:
        raise ExtractionGoldError(f"{label}.asserted has unsupported modality {asserted!r}")
    return GoldModality(
        asserted=asserted,  # type: ignore[arg-type]
        positive_anchors=_unique_texts(raw["positive_anchors"], f"{label}.positive_anchors", allow_empty=False),
        forbidden_assertions=_unique_texts(
            raw["forbidden_assertions"], f"{label}.forbidden_assertions", allow_empty=False
        ),
    )


def _parse_unit(value: object, label: str, events: tuple[GoldEvent, ...]) -> GoldAtomicFactUnit:
    raw = _mapping(value, label)
    required = {
        "unit_id",
        "source_event_indices",
        "role_action_object",
        "proper_entities",
        "speaker",
        "canonical_subject",
        "forbidden_propagation",
        "modality",
        "requires_self_contained_chain",
    }
    _exact_keys(raw, required, label)
    index_values = _list(raw["source_event_indices"], f"{label}.source_event_indices")
    if not index_values or any(type(item) is not int or item < 0 or item >= len(events) for item in index_values):
        raise ExtractionGoldError(f"{label}.source_event_indices must reference existing events")
    indices = tuple(index_values)
    if len(indices) != len(set(indices)):
        raise ExtractionGoldError(f"{label}.source_event_indices must not contain duplicates")
    speaker = _text(raw["speaker"], f"{label}.speaker")
    if speaker not in {events[index].speaker for index in indices}:
        raise ExtractionGoldError(f"{label}.speaker must match a referenced event")
    relation = _parse_relation(raw["role_action_object"], f"{label}.role_action_object")
    requires_chain = raw["requires_self_contained_chain"]
    if not isinstance(requires_chain, bool):
        raise ExtractionGoldError(f"{label}.requires_self_contained_chain must be boolean")
    if requires_chain and not (
        relation.roles and relation.actions and relation.objects and len(relation.ordered_anchors) >= 3
    ):
        raise ExtractionGoldError(f"{label}.role_action_object must define a complete chain")
    return GoldAtomicFactUnit(
        unit_id=_text(raw["unit_id"], f"{label}.unit_id"),
        source_event_indices=indices,
        role_action_object=relation,
        proper_entities=_unique_texts(raw["proper_entities"], f"{label}.proper_entities"),
        speaker=speaker,
        canonical_subject=_text(raw["canonical_subject"], f"{label}.canonical_subject"),
        forbidden_propagation=_unique_texts(raw["forbidden_propagation"], f"{label}.forbidden_propagation"),
        modality=_parse_modality(raw["modality"], f"{label}.modality"),
        requires_self_contained_chain=requires_chain,
    )


def _parse_case(value: object, label: str) -> ExtractionGoldCase:
    raw = _mapping(value, label)
    _exact_keys(raw, {"case_id", "experiment", "category", "events", "gold_units"}, label)
    experiment = _text(raw["experiment"], f"{label}.experiment")
    if experiment not in {"entities_hybrid_a", "proper_noun_prompt_b"}:
        raise ExtractionGoldError(f"{label}.experiment is unsupported")
    events: list[GoldEvent] = []
    for index, item in enumerate(_list(raw["events"], f"{label}.events")):
        event = _mapping(item, f"{label}.events[{index}]")
        _exact_keys(event, {"speaker", "text"}, f"{label}.events[{index}]")
        events.append(
            GoldEvent(
                speaker=_text(event["speaker"], f"{label}.events[{index}].speaker"),
                text=_text(event["text"], f"{label}.events[{index}].text"),
            )
        )
    if not events:
        raise ExtractionGoldError(f"{label}.events must not be empty")
    event_tuple = tuple(events)
    units = tuple(
        _parse_unit(item, f"{label}.gold_units[{index}]", event_tuple)
        for index, item in enumerate(_list(raw["gold_units"], f"{label}.gold_units"))
    )
    if not units:
        raise ExtractionGoldError(f"{label}.gold_units must not be empty")
    unit_ids = [unit.unit_id for unit in units]
    if len(unit_ids) != len(set(unit_ids)):
        raise ExtractionGoldError(f"{label}.gold_units unit_id values must be unique")
    return ExtractionGoldCase(
        case_id=_text(raw["case_id"], f"{label}.case_id"),
        experiment=experiment,  # type: ignore[arg-type]
        category=_text(raw["category"], f"{label}.category"),
        events=event_tuple,
        gold_units=units,
    )


def _parse_dedup_example(value: object, label: str) -> DedupExample:
    raw = _mapping(value, label)
    _exact_keys(raw, {"subject", "value", "proper_entities"}, label)
    return DedupExample(
        subject=_text(raw["subject"], f"{label}.subject"),
        value=_text(raw["value"], f"{label}.value"),
        proper_entities=_unique_texts(raw["proper_entities"], f"{label}.proper_entities"),
    )


def _parse_dedup_pair(value: object, label: str) -> DedupGoldPair:
    raw = _mapping(value, label)
    _exact_keys(raw, {"pair_id", "left", "right", "expected"}, label)
    expected = _text(raw["expected"], f"{label}.expected")
    if expected not in {"reuse", "distinct"}:
        raise ExtractionGoldError(f"{label}.expected must be reuse or distinct")
    left = _parse_dedup_example(raw["left"], f"{label}.left")
    right = _parse_dedup_example(raw["right"], f"{label}.right")
    overlap = set(left.proper_entities) & set(right.proper_entities)
    if expected == "reuse" and not overlap:
        raise ExtractionGoldError(f"{label} reuse pair must share a proper entity")
    if left == right:
        raise ExtractionGoldError(f"{label} must contain two different examples")
    return DedupGoldPair(
        pair_id=_text(raw["pair_id"], f"{label}.pair_id"),
        left=left,
        right=right,
        expected=expected,  # type: ignore[arg-type]
    )


def load_extraction_gold(path: Path) -> ExtractionGoldCorpus:
    """Load a strict extraction-v2 corpus from a UTF-8 JSON file."""
    try:
        raw_value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ExtractionGoldError(f"cannot load extraction gold {path}: {error}") from error
    raw = _mapping(raw_value, "corpus")
    _exact_keys(raw, {"schema_version", "data_classification", "cases", "dedup_pairs"}, "corpus")
    if raw["schema_version"] != 2:
        raise ExtractionGoldError("corpus.schema_version must be 2")
    data_classification = _text(raw["data_classification"], "corpus.data_classification")
    if data_classification not in {"synthetic_public", "private_external"}:
        raise ExtractionGoldError("corpus.data_classification must be synthetic_public or private_external")
    cases = tuple(
        _parse_case(item, f"corpus.cases[{index}]") for index, item in enumerate(_list(raw["cases"], "corpus.cases"))
    )
    pairs = tuple(
        _parse_dedup_pair(item, f"corpus.dedup_pairs[{index}]")
        for index, item in enumerate(_list(raw["dedup_pairs"], "corpus.dedup_pairs"))
    )
    case_ids = [case.case_id for case in cases]
    pair_ids = [pair.pair_id for pair in pairs]
    if len(case_ids) != len(set(case_ids)):
        raise ExtractionGoldError("corpus case_id values must be unique")
    if len(pair_ids) != len(set(pair_ids)):
        raise ExtractionGoldError("corpus pair_id values must be unique")
    return ExtractionGoldCorpus(2, data_classification, cases, pairs)


def _normalize(value: object) -> str:
    return "".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def _exact_entity(value: object) -> str:
    return unicodedata.normalize("NFC", str(value))


def _claim_value(claim: object, key: str, default: object = None) -> object:
    if isinstance(claim, Mapping):
        return claim.get(key, default)
    return getattr(claim, key, default)


def _claim_surface(claim: object) -> str:
    return _normalize(
        " ".join(
            str(_claim_value(claim, key, "") or "") for key in ("subject", "subject_entity_id", "predicate", "value")
        )
    )


def _claim_value_surface(claim: object) -> str:
    """Return the one field that must carry a self-contained atomic fact."""
    return _normalize(_claim_value(claim, "value", ""))


def _claim_indices(claim: object) -> frozenset[int]:
    value = _claim_value(claim, "source_event_indices")
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return frozenset()
    return frozenset(item for item in value if type(item) is int and item >= 0)


def _validate_claim_indices(case: ExtractionGoldCase, claims: Sequence[object]) -> None:
    for index, claim in enumerate(claims):
        raw = _claim_value(claim, "source_event_indices")
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
            raise ValueError(f"claims[{index}].source_event_indices must be an explicit list")
        indices = list(raw)
        if (
            not indices
            or len(indices) != len(set(indices))
            or any(type(item) is not int or item < 0 or item >= len(case.events) for item in indices)
        ):
            raise ValueError(f"claims[{index}].source_event_indices must reference existing events")


def _relevant_claims(unit: GoldAtomicFactUnit, claims: Sequence[object]) -> list[object]:
    required = set(unit.source_event_indices)
    return [claim for claim in claims if required == set(_claim_indices(claim))]


def _contains_all(surface: str, anchors: Sequence[str]) -> bool:
    return all(_normalize(anchor) in surface for anchor in anchors)


def _contains_in_order(surface: str, anchors: Sequence[str]) -> bool:
    position = 0
    for anchor in anchors:
        normalized = _normalize(anchor)
        found = surface.find(normalized, position)
        if found < 0:
            return False
        position = found + len(normalized)
    return True


def _best_unit(case: ExtractionGoldCase, claim: object) -> GoldAtomicFactUnit | None:
    indices = _claim_indices(claim)
    candidates = [unit for unit in case.gold_units if set(unit.source_event_indices) == set(indices)]
    if not candidates:
        return None
    surface = _claim_surface(claim)

    def rank(unit: GoldAtomicFactUnit) -> tuple[int, int, str]:
        anchors = unit.role_action_object.roles + unit.role_action_object.actions + unit.role_action_object.objects
        hits = sum(_normalize(anchor) in surface for anchor in anchors)
        entity_hits = sum(_normalize(entity) in surface for entity in unit.proper_entities)
        return (hits, entity_hits, unit.unit_id)

    return max(candidates, key=rank)


def _ratio_pass(values: Sequence[bool]) -> float:
    return sum(values) / len(values) if values else 1.0


def score_extraction_case(case: ExtractionGoldCase, claims: Sequence[object]) -> ExtractionCaseScore:
    """Score one extraction sample against atomic gold without fuzzy LLM judging."""
    _validate_claim_indices(case, claims)
    chain_units = [unit for unit in case.gold_units if unit.requires_self_contained_chain]
    direction_checks: list[bool] = []
    atomicity_checks: list[bool] = []
    for unit in chain_units:
        relevant = _relevant_claims(unit, claims)
        anchors = unit.role_action_object
        direction_checks.append(
            any(_contains_in_order(_claim_value_surface(claim), anchors.ordered_anchors) for claim in relevant)
        )
        all_chain_anchors = anchors.roles + anchors.actions + anchors.objects
        atomicity_checks.append(
            any(_contains_all(_claim_value_surface(claim), all_chain_anchors) for claim in relevant)
        )

    modality_checks: list[bool] = []
    for unit in case.gold_units:
        if unit.modality is None:
            continue
        surfaces = [_claim_surface(claim) for claim in _relevant_claims(unit, claims)]
        positive = any(_contains_all(surface, unit.modality.positive_anchors) for surface in surfaces)
        forbidden = any(
            _normalize(anchor) in surface for surface in surfaces for anchor in unit.modality.forbidden_assertions
        )
        modality_checks.append(positive and not forbidden)

    propagation_checks: list[bool] = []
    subject_checks: list[bool] = []
    for unit in case.gold_units:
        relevant = _relevant_claims(unit, claims)
        if unit.forbidden_propagation:
            propagation_checks.append(
                not any(
                    _normalize(anchor) in _claim_surface(claim)
                    for claim in relevant
                    for anchor in unit.forbidden_propagation
                )
            )
        if relevant:
            subject_checks.append(
                any(
                    _normalize(_claim_value(claim, "subject", _claim_value(claim, "subject_entity_id", "")))
                    == _normalize(unit.canonical_subject)
                    for claim in relevant
                )
            )
        else:
            subject_checks.append(False)

    true_positive = false_positive = false_negative = 0
    matched_units: set[str] = set()
    for claim in claims:
        matched_unit = _best_unit(case, claim)
        if matched_unit is None:
            entities = _claim_value(claim, "entities", ()) or ()
            if isinstance(entities, Sequence) and not isinstance(entities, (str, bytes)):
                false_positive += len({_exact_entity(item) for item in entities if _exact_entity(item)})
            continue
        matched_units.add(matched_unit.unit_id)
        expected = {_exact_entity(item) for item in matched_unit.proper_entities}
        raw_entities = _claim_value(claim, "entities", ()) or ()
        predicted = (
            {_exact_entity(item) for item in raw_entities if _exact_entity(item)}
            if isinstance(raw_entities, Sequence) and not isinstance(raw_entities, (str, bytes))
            else set()
        )
        true_positive += len(expected & predicted)
        false_positive += len(predicted - expected)
        false_negative += len(expected - predicted)
    for unit in case.gold_units:
        if unit.unit_id not in matched_units:
            false_negative += len(unit.proper_entities)
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 1.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return ExtractionCaseScore(
        relation_role_direction=_ratio_pass(direction_checks),
        modality_negative=_ratio_pass(modality_checks),
        entity_precision=precision,
        entity_recall=recall,
        entity_f1=f1,
        forbidden_propagation=_ratio_pass(propagation_checks),
        canonical_subject=_ratio_pass(subject_checks),
        chain_atomicity=_ratio_pass(atomicity_checks),
    )


def score_dedup_pairs(
    pairs: Sequence[DedupGoldPair],
    decisions: Mapping[str, Literal["reuse", "distinct"]],
) -> DedupPairScore:
    """Score dedup decisions, keeping unsafe false reuse visible as its own count."""
    expected_ids = {pair.pair_id for pair in pairs}
    decision_ids = set(decisions)
    missing = expected_ids - decision_ids
    extra = decision_ids - expected_ids
    if missing:
        raise ValueError(f"missing decisions for {sorted(missing)[0]}")
    if extra:
        raise ValueError(f"unknown decision for {sorted(extra)[0]}")
    if any(decision not in {"reuse", "distinct"} for decision in decisions.values()):
        raise ValueError("dedup decisions must be reuse or distinct")

    true_reuse = false_reuse = missed_reuse = true_distinct = 0
    for pair in pairs:
        predicted = decisions[pair.pair_id]
        if pair.expected == "reuse" and predicted == "reuse":
            true_reuse += 1
        elif pair.expected == "reuse":
            missed_reuse += 1
        elif predicted == "reuse":
            false_reuse += 1
        else:
            true_distinct += 1
    total = len(pairs)
    correct = true_reuse + true_distinct
    return DedupPairScore(
        total=total,
        correct=correct,
        false_reuse_count=false_reuse,
        reuse_precision=(true_reuse / (true_reuse + false_reuse) if true_reuse + false_reuse else 1.0),
        reuse_recall=(true_reuse / (true_reuse + missed_reuse) if true_reuse + missed_reuse else 1.0),
        distinct_recall=(true_distinct / (true_distinct + false_reuse) if true_distinct + false_reuse else 1.0),
        accuracy=correct / total if total else 1.0,
    )


def score_extraction_majority(
    case: ExtractionGoldCase,
    samples: Sequence[Sequence[object]],
) -> ExtractionMajorityScore:
    """Apply a strict majority vote to per-sample metric passes.

    A metric contributes a positive vote only when the individual sample reaches
    1.0. This prevents two complementary partial extractions from being combined
    into a pass that no single model response achieved.
    """
    if len(samples) < 3 or len(samples) % 2 == 0:
        raise ValueError("multi-sample scoring requires an odd number of at least three samples")
    scores = [score_extraction_case(case, sample) for sample in samples]
    metric_names = tuple(field.name for field in fields(ExtractionCaseScore))
    support = {name: sum(float(getattr(score, name)) == 1.0 for score in scores) / len(scores) for name in metric_names}
    majority = {name: float(value > 0.5) for name, value in support.items()}
    return ExtractionMajorityScore(len(samples), majority, support)
