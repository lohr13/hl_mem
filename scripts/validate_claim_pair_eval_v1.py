"""Validate the frozen claim-pair evaluation dataset without pytest."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evaluation" / "datasets" / "claim_pair_eval_v1.jsonl"
DATABASE = ROOT / "var" / "hl_mem.db"

EXPECTED_SLICES = {
    "same_subject_slot_high_cosine": 15,
    "same_subject_slot_mid_low_cosine": 15,
    "cross_subject_semantic": 15,
    "hard_negative": 25,
    "llm_paraphrase_positive": 10,
}
LABELS = {"equivalent", "compatible", "conflict", "unrelated", "uncertain"}
HEADER = re.compile(
    r"^# corpus_count=(?P<count>\d+) corpus_fingerprint=(?P<fingerprint>[0-9a-f]{64}) "
    r"method=sha256\(concat\(active_claim_ids_ordered_by_id\)\)$"
)


def _load_claims(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT id,subject_entity_id,predicate,value_json,canonical_slot "
        "FROM claims WHERE status='active' ORDER BY id"
    ).fetchall()
    return {str(row["id"]): dict(row) for row in rows}


def main() -> None:
    lines = DATASET.read_text(encoding="utf-8").splitlines()
    assert lines, "dataset is empty"
    header = HEADER.fullmatch(lines[0])
    assert header is not None, "invalid corpus fingerprint header"
    rows = [json.loads(line) for line in lines[1:] if line.strip()]
    assert len(rows) == 80, f"expected 80 pairs, got {len(rows)}"

    connection = sqlite3.connect(f"file:{DATABASE.as_posix()}?mode=ro", uri=True)
    try:
        claims = _load_claims(connection)
    finally:
        connection.close()
    ordered_ids = sorted(claims)
    fingerprint = hashlib.sha256("".join(ordered_ids).encode("utf-8")).hexdigest()
    assert int(header["count"]) == len(claims)
    assert header["fingerprint"] == fingerprint

    pair_ids: set[str] = set()
    unordered_pairs: set[tuple[str, str]] = set()
    claim_splits: defaultdict[str, set[str]] = defaultdict(set)
    source_counts: Counter[str] = Counter()
    split_counts: Counter[str] = Counter()

    for index, row in enumerate(rows, 1):
        expected_pair_id = f"cp-{index:04d}"
        assert row["pair_id"] == expected_pair_id
        assert row["pair_id"] not in pair_ids
        pair_ids.add(row["pair_id"])
        source_counts[row["source_slice"]] += 1
        assert row["label"] in LABELS
        assert row["merge_safe"] is (row["label"] == "equivalent")
        if row["label"] == "conflict":
            assert row["conflict_subtype"] in {"contradiction", "state_change"}
        else:
            assert row["conflict_subtype"] is None
        assert isinstance(row["rationale"], str) and row["rationale"].strip()
        assert row["split"] in {"dev", "test"}
        split_counts[row["split"]] += 1

        left = row["left"]
        right = row["right"]
        for side in (left, right):
            assert set(side) == {"claim_id", "subject", "predicate", "value", "canonical_slot"}
            claim_id = str(side["claim_id"])
            claim_splits[claim_id].add(row["split"])
            if claim_id.startswith("synthetic:"):
                continue
            assert claim_id in claims, f"unknown active claim: {claim_id}"
            stored = claims[claim_id]
            assert side["subject"] == stored["subject_entity_id"]
            assert side["predicate"] == stored["predicate"]
            assert side["value"] == json.loads(stored["value_json"])
            assert side["canonical_slot"] == stored["canonical_slot"]

        key = tuple(sorted((str(left["claim_id"]), str(right["claim_id"]))))
        assert key not in unordered_pairs, f"duplicate pair: {key}"
        unordered_pairs.add(key)
        features = row["mining_features"]
        assert set(features) == {"cosine", "lexical_overlap"}
        assert features["cosine"] is None or -1.0 <= float(features["cosine"]) <= 1.0
        assert 0.0 <= float(features["lexical_overlap"]) <= 1.0

        if row["source_slice"] == "same_subject_slot_high_cosine":
            assert left["subject"] == right["subject"]
            assert left["canonical_slot"] is not None
            assert left["canonical_slot"] == right["canonical_slot"]
            assert float(features["cosine"]) >= 0.88
        elif row["source_slice"] == "same_subject_slot_mid_low_cosine":
            assert left["subject"] == right["subject"]
            assert left["canonical_slot"] is not None
            assert left["canonical_slot"] == right["canonical_slot"]
            assert 0.75 <= float(features["cosine"]) < 0.88
        elif row["source_slice"] == "cross_subject_semantic":
            assert left["subject"] != right["subject"]
        elif row["source_slice"] == "llm_paraphrase_positive":
            assert right["claim_id"].startswith(f"synthetic:{left['claim_id']}:")
            assert row["label"] == "equivalent"

    assert source_counts == Counter(EXPECTED_SLICES), source_counts
    assert split_counts == Counter({"dev": 48, "test": 32}), split_counts
    leaking = {claim_id: splits for claim_id, splits in claim_splits.items() if len(splits) > 1}
    assert not leaking, f"claim split leakage: {leaking}"
    print(
        json.dumps(
            {
                "pairs": len(rows),
                "corpus_count": len(claims),
                "corpus_fingerprint": fingerprint,
                "source_counts": source_counts,
                "split_counts": split_counts,
                "label_counts": Counter(row["label"] for row in rows),
            },
            ensure_ascii=False,
            default=dict,
        )
    )


if __name__ == "__main__":
    main()
