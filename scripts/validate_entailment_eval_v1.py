"""Validate extraction_gold_v1 and entailment_eval_v1 without project imports."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
TESTSET_PATH = ROOT / "scripts" / "extraction_testset.jsonl"
PREDICTIONS_PATH = ROOT / "scripts" / "after_qwen_v0211.jsonl"
GOLD_PATH = ROOT / "evaluation" / "datasets" / "extraction_gold_v1.jsonl"
ENTAILMENT_PATH = ROOT / "evaluation" / "datasets" / "entailment_eval_v1.jsonl"

EVENT_KEYS = {
    "event_id",
    "category",
    "text",
    "text_sha256",
    "should_memorize",
    "gold_claims",
    "split",
}
GOLD_CLAIM_KEYS = {"gold_claim_id", "subject", "predicate", "value", "scope"}
PAIR_KEYS = {
    "pair_id",
    "event_id",
    "candidate_source",
    "claim",
    "support_label",
    "memory_worthy",
    "rationale",
    "split",
}
CLAIM_KEYS = {"subject", "predicate", "value"}
SUPPORT_LABELS = {"entailed", "partially_entailed", "contradicted", "unsupported"}
SOURCES = {"gold", "qwen_after_v0211", "mutation"}
SPLITS = {"dev", "test"}
SCOPES = {"permanent", "temporal"}
PREDICATES = {"偏好", "使用", "状态", "身份", "配置", "计划", "事实"}
NESTED_PREFIX = '{"text": "'


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise AssertionError(f"missing dataset: {path.relative_to(ROOT)}")
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"{path.name}:{line_no}: invalid JSON: {exc}") from exc
        assert isinstance(value, dict), f"{path.name}:{line_no}: row must be an object"
        rows.append(value)
    return rows


def extract_text(content: str) -> str:
    """Decode the nested content JSON, tolerating benchmark truncation."""
    try:
        value = json.loads(content)
        assert isinstance(value, dict) and isinstance(value.get("text"), str)
        return value["text"]
    except json.JSONDecodeError:
        assert content.startswith(NESTED_PREFIX), "truncated content has an unexpected shape"
        encoded = content[len(NESTED_PREFIX) :]
        while encoded.endswith("\\"):
            encoded = encoded[:-1]
        value = json.loads(f'"{encoded}"')
        assert isinstance(value, str)
        return value


def claim_key(event_id: str, claim: dict[str, Any]) -> tuple[str, str, str, str]:
    return (event_id, claim["subject"], claim["predicate"], claim["value"])


def is_single_variable_mutation(
    mutation: dict[str, str],
    gold_claims: list[dict[str, Any]],
) -> bool:
    """Accept one-field changes or a self-contained subject substitution in subject+value."""
    for gold in gold_claims:
        differences = [key for key in CLAIM_KEYS if mutation[key] != gold[key]]
        if len(differences) == 1:
            return True
        if set(differences) == {"subject", "value"} and mutation["predicate"] == gold["predicate"]:
            old_subject = gold["subject"]
            new_subject = mutation["subject"]
            if old_subject in gold["value"] and mutation["value"] == gold["value"].replace(
                old_subject, new_subject, 1
            ):
                return True
    return False


def main() -> None:
    raw_events = read_jsonl(TESTSET_PATH)
    predictions = read_jsonl(PREDICTIONS_PATH)
    gold_rows = read_jsonl(GOLD_PATH)
    pair_rows = read_jsonl(ENTAILMENT_PATH)

    assert len(raw_events) == 50, f"expected 50 source events, got {len(raw_events)}"
    assert len(predictions) == 50, f"expected 50 prediction rows, got {len(predictions)}"
    assert len(gold_rows) == 50, f"expected 50 gold event rows, got {len(gold_rows)}"
    assert 100 <= len(pair_rows) <= 150, f"expected 100-150 pairs, got {len(pair_rows)}"

    event_by_id = {row["id"]: row for row in raw_events}
    assert len(event_by_id) == 50, "source event IDs must be unique"
    assert [row["event_id"] for row in gold_rows] == [row["id"] for row in raw_events], (
        "gold rows must preserve source event order"
    )

    expected_test_counts = {
        "user_pref": 4,
        "project_config": 4,
        "tool_workflow": 4,
        "status_report": 2,
        "chat_confirm": 2,
        "long_content": 4,
    }
    split_by_event: dict[str, str] = {}
    gold_claims_by_event: dict[str, list[dict[str, Any]]] = {}
    gold_by_id: dict[str, dict[str, Any]] = {}
    expected_gold_pairs: Counter[tuple[str, str, str, str]] = Counter()

    for row in gold_rows:
        assert set(row) == EVENT_KEYS, f"bad event keys: {row.get('event_id')}"
        event_id = row["event_id"]
        source = event_by_id[event_id]
        text = extract_text(source["content"])
        assert row["category"] == source["category"], f"category mismatch: {event_id}"
        assert row["text"] == text, f"text mismatch: {event_id}"
        assert row["text_sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest(), (
            f"text hash mismatch: {event_id}"
        )
        assert row["split"] in SPLITS, f"bad split: {event_id}"
        assert isinstance(row["should_memorize"], bool), f"bad should_memorize: {event_id}"
        assert isinstance(row["gold_claims"], list), f"bad gold_claims: {event_id}"
        assert row["should_memorize"] == bool(row["gold_claims"]), (
            f"should_memorize must equal bool(gold_claims): {event_id}"
        )
        split_by_event[event_id] = row["split"]
        gold_claims_by_event[event_id] = row["gold_claims"]

        for index, claim in enumerate(row["gold_claims"], 1):
            assert set(claim) == GOLD_CLAIM_KEYS, f"bad gold claim keys: {event_id}"
            expected_id = f"{event_id}:g{index:02d}"
            assert claim["gold_claim_id"] == expected_id, f"bad gold claim id: {expected_id}"
            assert claim["gold_claim_id"] not in gold_by_id, f"duplicate gold ID: {expected_id}"
            assert claim["predicate"] in PREDICATES, f"bad predicate: {expected_id}"
            assert claim["scope"] in SCOPES, f"bad scope: {expected_id}"
            for key in ("subject", "value"):
                assert isinstance(claim[key], str) and claim[key].strip(), f"empty {key}: {expected_id}"
            gold_by_id[claim["gold_claim_id"]] = claim
            expected_gold_pairs[claim_key(event_id, claim)] += 1

    split_counts = Counter(row["split"] for row in gold_rows)
    assert split_counts == Counter({"dev": 30, "test": 20}), f"bad event split: {split_counts}"
    for category, expected_test in expected_test_counts.items():
        actual = sum(
            row["category"] == category and row["split"] == "test" for row in gold_rows
        )
        assert actual == expected_test, f"bad test split for {category}: {actual}"

    expected_pair_ids = [f"ent-{index:03d}" for index in range(1, len(pair_rows) + 1)]
    assert [row["pair_id"] for row in pair_rows] == expected_pair_ids, "pair IDs/order are not canonical"

    pair_source_counts: Counter[str] = Counter()
    label_counts: Counter[str] = Counter()
    actual_gold_pairs: Counter[tuple[str, str, str, str]] = Counter()
    actual_qwen_pairs: Counter[tuple[str, str, str, str]] = Counter()
    mutation_claims: set[tuple[str, str, str, str]] = set()

    for row in pair_rows:
        pair_id = row["pair_id"]
        assert set(row) == PAIR_KEYS, f"bad pair keys: {pair_id}"
        event_id = row["event_id"]
        assert event_id in event_by_id, f"unknown event: {pair_id}"
        assert row["candidate_source"] in SOURCES, f"bad source: {pair_id}"
        assert row["support_label"] in SUPPORT_LABELS, f"bad label: {pair_id}"
        assert isinstance(row["memory_worthy"], bool), f"bad memory_worthy: {pair_id}"
        assert isinstance(row["rationale"], str) and row["rationale"].strip(), f"empty rationale: {pair_id}"
        assert row["split"] == split_by_event[event_id], f"pair/event split mismatch: {pair_id}"
        assert isinstance(row["claim"], dict) and set(row["claim"]) == CLAIM_KEYS, f"bad claim: {pair_id}"
        for key, value in row["claim"].items():
            assert isinstance(value, str) and value.strip(), f"empty claim {key}: {pair_id}"

        source_name = row["candidate_source"]
        key = claim_key(event_id, row["claim"])
        pair_source_counts[source_name] += 1
        label_counts[row["support_label"]] += 1

        if source_name == "gold":
            assert row["support_label"] == "entailed", f"gold must be entailed: {pair_id}"
            assert row["memory_worthy"] is True, f"gold must be memory-worthy: {pair_id}"
            actual_gold_pairs[key] += 1
        elif source_name == "qwen_after_v0211":
            actual_qwen_pairs[key] += 1
        else:
            assert row["support_label"] in {"contradicted", "unsupported"}, (
                f"mutation must be contradicted/unsupported: {pair_id}"
            )
            assert row["memory_worthy"] is False, f"mutation must not be memory-worthy: {pair_id}"
            assert key not in mutation_claims, f"duplicate mutation: {pair_id}"
            assert is_single_variable_mutation(row["claim"], gold_claims_by_event[event_id]), (
                f"mutation is not derived by one semantic variable from an event gold claim: {pair_id}"
            )
            mutation_claims.add(key)

    assert actual_gold_pairs == expected_gold_pairs, "gold entailment pairs do not exactly match gold claims"

    expected_qwen_pairs: Counter[tuple[str, str, str, str]] = Counter()
    for row in predictions:
        for claim in row.get("claims_data", []):
            expected_qwen_pairs[claim_key(row["event_id"], claim)] += 1
    assert sum(expected_qwen_pairs.values()) == 37, "prediction corpus must contain 37 claims"
    assert actual_qwen_pairs == expected_qwen_pairs, "qwen entailment pairs do not exactly match predictions"
    assert pair_source_counts["qwen_after_v0211"] == 37
    assert 10 <= pair_source_counts["mutation"] <= 15, f"bad mutation count: {pair_source_counts}"
    assert set(label_counts) == SUPPORT_LABELS, f"all support labels must be represented: {label_counts}"
    pair_split_counts = Counter(row["split"] for row in pair_rows)
    expected_test_pairs = round(len(pair_rows) * 0.40)
    assert pair_split_counts["test"] == expected_test_pairs, (
        f"pair split must be 60/40 after event grouping: {pair_split_counts}"
    )

    print(f"Validated {len(gold_rows)} events and {len(pair_rows)} entailment pairs")
    print(f"Gold claims: {len(gold_by_id)}")
    print(f"Sources: {dict(sorted(pair_source_counts.items()))}")
    print(f"Labels: {dict(sorted(label_counts.items()))}")
    print(f"Event splits: {dict(sorted(split_counts.items()))}")
    print(f"Pair splits: {dict(sorted(pair_split_counts.items()))}")


if __name__ == "__main__":
    main()
