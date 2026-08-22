"""Isolated corpus, gold, and compact-run projection for state experiments."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from hl_mem.evaluation.state_protocol import coordinate_key


def _normalized_text(value: object) -> str:
    return "".join(
        character for character in unicodedata.normalize("NFKC", str(value)).casefold() if not character.isspace()
    )


def _content_anchors(value: object) -> set[str]:
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    ascii_tokens = {
        token
        for token in re.findall(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", text)
        if len(token) >= 3 or any(character.isdigit() for character in token)
    }
    han_anchors = {
        run[index : index + 2] for run in re.findall(r"[\u3400-\u9fff]+", text) for index in range(len(run) - 1)
    }
    return ascii_tokens | han_anchors


def _source_indices(value: object, label: str) -> tuple[int, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{label} source_event_indices must be an integer array")
    indices = list(value)
    if not indices or any(isinstance(index, bool) or not isinstance(index, int) or index < 0 for index in indices):
        raise ValueError(f"{label} source_event_indices must be a non-empty non-negative integer array")
    return tuple(sorted(set(indices)))


def _event_text(event: Mapping[str, Any]) -> str:
    content = event.get("content")
    if isinstance(content, Mapping):
        return str(content.get("text") or "")
    return str(content or "")


def _corpus_projection(corpus_records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    samples: dict[str, dict[str, Any]] = {}
    for record in corpus_records:
        sample_id = str(record.get("sample_id") or record.get("bundle_id") or "").strip()
        if not sample_id or sample_id in samples:
            raise ValueError(f"corpus sample id must be non-blank and unique: {sample_id!r}")
        events = record.get("events")
        if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
            raise ValueError(f"corpus {sample_id} events must be an array")
        normalized_events: dict[int, str] = {}
        for fallback_index, event in enumerate(events):
            if not isinstance(event, Mapping):
                raise ValueError(f"corpus {sample_id} events must contain objects")
            event_index = event.get("event_index", fallback_index)
            if isinstance(event_index, bool) or not isinstance(event_index, int) or event_index < 0:
                raise ValueError(f"corpus {sample_id} event_index must be a non-negative integer")
            if event_index in normalized_events:
                raise ValueError(f"corpus {sample_id} event_index must be unique: {event_index}")
            normalized_events[event_index] = _normalized_text(_event_text(event))
        samples[sample_id] = {
            "category": str(record.get("category") or "unknown"),
            "subtype": str(record.get("subtype") or "unknown"),
            "events": normalized_events,
        }
    return samples


def _gold_projection(gold_records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    atomic_ids: set[str] = set()
    assertions: dict[str, dict[str, Any]] = {}
    by_sample: dict[str, list[str]] = {}
    sample_metadata: dict[str, dict[str, str]] = {}
    coordinates: dict[str, str] = {}
    non_state_ids: set[str] = set()
    expected_edges: set[tuple[str, str]] = set()
    current_ids: set[str] = set()
    historical_ids: set[str] = set()
    counterexample_ids: set[str] = set()
    counterexample_samples: dict[str, str] = {}
    for record in gold_records:
        sample_id = str(record.get("sample_id") or record.get("bundle_id") or "").strip()
        if not sample_id or sample_id in by_sample:
            raise ValueError(f"gold sample id must be non-blank and unique: {sample_id!r}")
        claims = record.get("atomic_claims")
        if not isinstance(claims, Sequence) or isinstance(claims, (str, bytes)):
            raise ValueError(f"gold {sample_id} atomic_claims must be an array")
        by_sample[sample_id] = []
        sample_metadata[sample_id] = {
            "category": str(record.get("category") or "unknown"),
            "subtype": str(record.get("subtype") or "unknown"),
        }
        for claim in claims:
            if not isinstance(claim, Mapping):
                raise ValueError(f"gold {sample_id} atomic_claims must contain objects")
            assertion_id = str(claim.get("assertion_id") or "")
            if not assertion_id or assertion_id in atomic_ids:
                raise ValueError(f"gold assertion_id must be non-blank and unique: {assertion_id!r}")
            atomic_ids.add(assertion_id)
            source_indices = _source_indices(claim.get("source_event_indices"), f"gold {assertion_id}")
            state_value = str(claim.get("state_value") or "").strip()
            if not state_value:
                raise ValueError(f"gold {assertion_id} state_value must be non-blank")
            assertions[assertion_id] = {
                "assertion_id": assertion_id,
                "sample_id": sample_id,
                "source_event_indices": source_indices,
                "state_value": state_value,
            }
            by_sample[sample_id].append(assertion_id)
            coordinate = claim.get("coordinate")
            if isinstance(coordinate, Mapping):
                coordinates[assertion_id] = coordinate_key(coordinate)
            else:
                non_state_ids.add(assertion_id)
            if record.get("counterexample_zero_supersede") is True:
                counterexample_ids.add(assertion_id)
                counterexample_samples[assertion_id] = sample_id
        for edge in record.get("expected_supersede_edges") or ():
            if not isinstance(edge, Sequence) or len(edge) != 2:
                raise ValueError(f"gold {sample_id} supersede edges must be pairs")
            expected_edges.add((str(edge[0]), str(edge[1])))
        current_ids.update(str(value) for value in record.get("current_assertion_ids") or ())
        historical_ids.update(str(value) for value in record.get("historical_assertion_ids") or ())
    return {
        "atomic_ids": atomic_ids,
        "assertions": assertions,
        "by_sample": by_sample,
        "sample_metadata": sample_metadata,
        "coordinates": coordinates,
        "non_state_ids": non_state_ids,
        "expected_edges": expected_edges,
        "current_ids": current_ids,
        "historical_ids": historical_ids,
        "counterexample_ids": counterexample_ids,
        "counterexample_samples": counterexample_samples,
    }


def _run_projection(run: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    atomic_ids: set[str] = set()
    assertions: dict[str, dict[str, Any]] = {}
    by_sample: dict[str, list[str]] = {}
    coordinates: dict[str, str] = {}
    non_state_ids: set[str] = set()
    coordinate_occurrences: list[tuple[str, str]] = []
    claim_count = 0
    for sample in run:
        if not isinstance(sample, Mapping):
            raise ValueError("candidate run samples must be objects")
        sample_id = str(sample.get("sample_id") or "").strip()
        if not sample_id or sample_id in by_sample:
            raise ValueError(f"candidate sample id must be non-blank and unique: {sample_id!r}")
        claims = sample.get("claims")
        if not isinstance(claims, Sequence) or isinstance(claims, (str, bytes)):
            raise ValueError("candidate run claims must be an array")
        by_sample[sample_id] = []
        claim_count += len(claims)
        for claim in claims:
            if not isinstance(claim, Mapping):
                raise ValueError("candidate run claims must contain objects")
            assertion_id = str(claim.get("assertion_id") or "")
            if not assertion_id or assertion_id in atomic_ids:
                raise ValueError(f"candidate assertion_id must be non-blank and unique: {assertion_id!r}")
            atomic_ids.add(assertion_id)
            raw_claim = claim.get("claim")
            if not isinstance(raw_claim, Mapping):
                raise ValueError(f"candidate {assertion_id} claim must be an object")
            source_indices = _source_indices(raw_claim.get("source_event_indices"), f"candidate {assertion_id}")
            value = str(raw_claim.get("value") or "").strip()
            evidence_quote = str(raw_claim.get("evidence_quote") or "").strip()
            if not value or not evidence_quote:
                raise ValueError(f"candidate {assertion_id} value and evidence_quote must be non-blank")
            projection = claim.get("projection")
            coordinate = projection.get("coordinate") if isinstance(projection, Mapping) else None
            assertions[assertion_id] = {
                "assertion_id": assertion_id,
                "sample_id": sample_id,
                "source_event_indices": source_indices,
                "value": value,
                "evidence_quote": evidence_quote,
            }
            by_sample[sample_id].append(assertion_id)
            if isinstance(coordinate, Mapping):
                stable_coordinate_key = coordinate_key(coordinate)
                coordinates[assertion_id] = stable_coordinate_key
                coordinate_occurrences.append((sample_id, stable_coordinate_key))
            else:
                non_state_ids.add(assertion_id)
    return {
        "atomic_ids": atomic_ids,
        "assertions": assertions,
        "by_sample": by_sample,
        "coordinates": coordinates,
        "non_state_ids": non_state_ids,
        "coordinate_occurrences": coordinate_occurrences,
        "claim_count": claim_count,
    }


def _semantic_match_reason(
    gold_assertion: Mapping[str, Any],
    candidate_assertion: Mapping[str, Any],
    corpus_sample: Mapping[str, Any] | None,
) -> str | None:
    if gold_assertion["source_event_indices"] != candidate_assertion["source_event_indices"]:
        return "source_event_mismatch"
    state_value = _normalized_text(gold_assertion["state_value"])
    value = _normalized_text(candidate_assertion["value"])
    evidence = _normalized_text(candidate_assertion["evidence_quote"])
    literal_state_value = True
    if corpus_sample is not None:
        events = corpus_sample["events"]
        selected_events = [events.get(index, "") for index in candidate_assertion["source_event_indices"]]
        if any(not event for event in selected_events):
            return "source_event_mismatch"
        source_text = "\n".join(selected_events)
        if not evidence or evidence not in source_text:
            return "evidence_ungrounded"
        literal_state_value = bool(state_value and state_value in source_text)
    if literal_state_value and state_value not in value:
        return "state_value_mismatch"
    if literal_state_value and state_value not in evidence:
        return "evidence_value_mismatch"
    if not literal_state_value and not (
        _content_anchors(candidate_assertion["value"]) & _content_anchors(candidate_assertion["evidence_quote"])
    ):
        return "value_evidence_mismatch"
    return None


def _match_assertions(
    gold: Mapping[str, Any],
    candidate: Mapping[str, Any],
    corpus: Mapping[str, Any],
) -> dict[str, Any]:
    candidate_to_gold: dict[str, str] = {}
    semantic_rejections: Counter[str] = Counter()
    gold_zero_false_positives = 0
    for sample_id in sorted(set(gold["by_sample"]) | set(candidate["by_sample"])):
        gold_ids = sorted(gold["by_sample"].get(sample_id, ()))
        candidate_ids = sorted(candidate["by_sample"].get(sample_id, ()))
        corpus_sample = corpus.get(sample_id)
        adjacency: dict[str, list[str]] = {}
        reasons: dict[tuple[str, str], str] = {}
        for candidate_id in candidate_ids:
            compatible: list[str] = []
            for gold_id in gold_ids:
                reason = _semantic_match_reason(
                    gold["assertions"][gold_id], candidate["assertions"][candidate_id], corpus_sample
                )
                if reason is None:
                    compatible.append(gold_id)
                else:
                    reasons[(candidate_id, gold_id)] = reason
            adjacency[candidate_id] = compatible

        gold_to_candidate: dict[str, str] = {}

        def assign(candidate_id: str, visited: set[str]) -> bool:
            for gold_id in adjacency[candidate_id]:
                if gold_id in visited:
                    continue
                visited.add(gold_id)
                previous = gold_to_candidate.get(gold_id)
                if previous is None or assign(previous, visited):
                    gold_to_candidate[gold_id] = candidate_id
                    return True
            return False

        for candidate_id in sorted(candidate_ids, key=lambda value: (len(adjacency[value]), value)):
            assign(candidate_id, set())
        for gold_id, candidate_id in gold_to_candidate.items():
            candidate_to_gold[candidate_id] = gold_id
        unmatched_candidates = [candidate_id for candidate_id in candidate_ids if candidate_id not in candidate_to_gold]
        if not gold_ids:
            gold_zero_false_positives += len(unmatched_candidates)
            semantic_rejections["gold_zero"] += len(unmatched_candidates)
            continue
        reason_priority = (
            "evidence_ungrounded",
            "state_value_mismatch",
            "evidence_value_mismatch",
            "value_evidence_mismatch",
            "source_event_mismatch",
        )
        for candidate_id in unmatched_candidates:
            if adjacency[candidate_id]:
                semantic_rejections["duplicate_semantic_match"] += 1
                continue
            candidate_reasons = {reasons[(candidate_id, gold_id)] for gold_id in gold_ids}
            semantic_rejections[next(reason for reason in reason_priority if reason in candidate_reasons)] += 1
    matched_gold = set(candidate_to_gold.values())
    return {
        "candidate_to_gold": candidate_to_gold,
        "unmatched_candidate_ids": set(candidate["atomic_ids"]) - set(candidate_to_gold),
        "unmatched_gold_ids": set(gold["atomic_ids"]) - matched_gold,
        "matched_gold_ids": matched_gold,
        "layout_remapped_matches": sum(candidate_id != gold_id for candidate_id, gold_id in candidate_to_gold.items()),
        "identity_matches": sum(candidate_id == gold_id for candidate_id, gold_id in candidate_to_gold.items()),
        "semantic_rejections": dict(sorted(semantic_rejections.items())),
        "gold_zero_false_positives": gold_zero_false_positives,
    }
