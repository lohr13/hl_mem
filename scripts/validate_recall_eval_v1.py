"""Validate the frozen production-backed recall evaluation dataset.

This is intentionally a standalone standard-library check.  It never imports
hl_mem and opens the production SQLite database in read-only/query-only mode.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "var" / "hl_mem.db"
DATASET = ROOT / "evaluation" / "datasets" / "recall_eval_v1.jsonl"
HEADER_RE = re.compile(
    r"^# corpus_count=(\d+) corpus_fingerprint=([0-9a-f]{64}) "
    r"method=sha256\(concat\(active_claim_ids_ordered_by_id\)\)$"
)
UUID_RE = re.compile(r"^[0-9a-f]{32}$")
QUERY_TYPES = {
    "normal",
    "deep_paraphrase",
    "entity_name",
    "path_version",
    "multi_gold",
    "hard_no_answer",
}
NEW_TYPE_COUNTS = {
    "normal": 10,
    "deep_paraphrase": 7,
    "entity_name": 5,
    "path_version": 5,
    "multi_gold": 3,
    "hard_no_answer": 20,
}
EXPECTED_FIELDS = {
    "id",
    "query",
    "query_type",
    "intent",
    "gold_ids",
    "gold_groups",
    "no_answer",
    "split",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def load_corpus() -> tuple[list[str], set[str]]:
    uri = f"file:{DATABASE.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        ids = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM claims WHERE status='active' ORDER BY id"
            )
        ]
    finally:
        connection.close()
    return ids, set(ids)


def main() -> None:
    require(DATASET.exists(), f"dataset missing: {DATASET}")
    lines = DATASET.read_text(encoding="utf-8").splitlines()
    require(lines, "dataset is empty")

    header_match = HEADER_RE.fullmatch(lines[0])
    require(header_match is not None, "invalid or missing corpus fingerprint header")
    rows = [json.loads(line) for line in lines[1:] if line.strip()]
    require(len(rows) == 80, f"expected 80 rows, found {len(rows)}")

    corpus_ids, active_ids = load_corpus()
    actual_fingerprint = hashlib.sha256("".join(corpus_ids).encode()).hexdigest()
    require(int(header_match.group(1)) == len(corpus_ids), "corpus count drifted")
    require(header_match.group(2) == actual_fingerprint, "corpus fingerprint drifted")

    expected_ids = [f"rq-{number:03d}" for number in range(1, 81)]
    require([row.get("id") for row in rows] == expected_ids, "row IDs/order are invalid")
    require(len({row["query"] for row in rows}) == 80, "queries must be unique")

    split_counts: Counter[str] = Counter()
    new_type_counts: Counter[str] = Counter()
    gold_to_rows: dict[str, set[str]] = defaultdict(set)
    row_by_id = {row["id"]: row for row in rows}

    for row in rows:
        row_id = row["id"]
        require(set(row) == EXPECTED_FIELDS, f"{row_id}: unexpected field set")
        require(isinstance(row["query"], str) and row["query"].strip(), f"{row_id}: empty query")
        require(row["query_type"] in QUERY_TYPES, f"{row_id}: invalid query_type")
        require(row["intent"] == "current_state", f"{row_id}: invalid intent")
        require(row["split"] in {"dev", "test"}, f"{row_id}: invalid split")
        require(isinstance(row["no_answer"], bool), f"{row_id}: no_answer must be bool")
        require(isinstance(row["gold_ids"], list), f"{row_id}: gold_ids must be a list")
        require(isinstance(row["gold_groups"], list), f"{row_id}: gold_groups must be a list")

        split_counts[row["split"]] += 1
        if int(row_id[-3:]) > 30:
            new_type_counts[row["query_type"]] += 1

        if row["no_answer"]:
            require(not row["gold_ids"], f"{row_id}: no-answer row has gold_ids")
            require(not row["gold_groups"], f"{row_id}: no-answer row has gold_groups")
            require(row["query_type"] == "hard_no_answer", f"{row_id}: no-answer type mismatch")
            continue

        require(row["gold_ids"], f"{row_id}: answerable row has no gold_ids")
        require(
            len(row["gold_ids"]) == len(row["gold_groups"]),
            f"{row_id}: one representative gold_id is required per gold_group",
        )
        require(
            row["query_type"] != "multi_gold" or len(row["gold_groups"]) >= 2,
            f"{row_id}: multi_gold needs at least two answer units",
        )
        for representative, group in zip(row["gold_ids"], row["gold_groups"], strict=True):
            require(isinstance(group, list) and group, f"{row_id}: empty gold group")
            require(representative in group, f"{row_id}: representative missing from group")
            require(len(group) == len(set(group)), f"{row_id}: duplicate ID inside gold group")
            for claim_id in group:
                require(UUID_RE.fullmatch(claim_id) is not None, f"{row_id}: invalid claim UUID")
                require(claim_id in active_ids, f"{row_id}: gold claim is not active: {claim_id}")
                gold_to_rows[claim_id].add(row_id)

    require(split_counts == {"dev": 48, "test": 32}, f"invalid split counts: {split_counts}")
    require(new_type_counts == NEW_TYPE_COUNTS, f"invalid new-query quotas: {new_type_counts}")

    # Any queries connected through an acceptable gold claim must stay in one
    # split, preventing synonymous/equivalent questions from leaking.
    for claim_id, row_ids in gold_to_rows.items():
        splits = {row_by_id[row_id]["split"] for row_id in row_ids}
        require(len(splits) == 1, f"split leakage through gold claim {claim_id}: {sorted(row_ids)}")

    print(
        json.dumps(
            {
                "status": "ok",
                "rows": len(rows),
                "answerable": sum(not row["no_answer"] for row in rows),
                "no_answer": sum(row["no_answer"] for row in rows),
                "split_counts": dict(split_counts),
                "new_type_counts": dict(new_type_counts),
                "corpus_count": len(corpus_ids),
                "corpus_fingerprint": actual_fingerprint,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
