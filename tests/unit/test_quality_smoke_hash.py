from __future__ import annotations

import json

import pytest

from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.storage.database import Database
from scripts.run_quality_smoke import (
    BASELINE_SCHEMA_VERSION,
    HASH_ALGORITHM,
    compare_baseline,
    dataset_hash,
    run_case,
    seed_case,
    write_baseline,
)


def test_dataset_hash_normalizes_utf8_newlines(tmp_path) -> None:
    hashes = []
    for index, newline in enumerate(("\n", "\r\n", "\r")):
        dataset = tmp_path / f"dataset-{index}.jsonl"
        dataset.write_bytes(f'{{"text":"你好"}}{newline}{{"text":"world"}}{newline}'.encode("utf-8"))
        hashes.append(dataset_hash(dataset))

    assert len(set(hashes)) == 1

    changed = tmp_path / "changed.jsonl"
    changed.write_text('{"text":"你好"}\n{"text":"WORLD"}\n', encoding="utf-8", newline="")
    assert dataset_hash(changed) != hashes[0]


def test_write_baseline_records_hash_contract(tmp_path) -> None:
    baseline = tmp_path / "baseline.json"

    write_baseline(baseline, "digest", {"mrr": 1.0}, {"case": {"mrr": 1.0}})

    payload = json.loads(baseline.read_text(encoding="utf-8"))
    assert payload["schema_version"] == BASELINE_SCHEMA_VERSION
    assert payload["hash_algorithm"] == HASH_ALGORITHM


def test_seed_case_marks_fixture_relations_as_deterministic(tmp_path) -> None:
    database = Database(tmp_path / "quality-smoke.db")
    case = {
        "id": "relation-provenance",
        "input": {
            "memories": [
                {"id": "source-event", "text": "记住 压测结果支持启用缓存。"},
                {"id": "target-event", "text": "记住 启用缓存可以降低接口延迟。"},
            ],
            "relation": {
                "from_event_id": "source-event",
                "to_event_id": "target-event",
                "type": "supports",
            },
        },
    }
    try:
        connection = database.open()
        seed_case(connection, case, FakeEmbedder(dim=64))
        relation = connection.execute("SELECT provenance,proposal_id FROM memory_relations").fetchone()
    finally:
        database.close()

    assert tuple(relation) == ("deterministic", None)


def test_relation_discovery_case_scores_a_pending_audit_proposal(tmp_path) -> None:
    database_path = tmp_path / "relation-discovery.db"
    case = {
        "id": "relation-discovery",
        "type": "relation_discovery",
        "input": {
            "memories": [
                {"id": "source-event", "text": "记住 压测结果支持启用缓存。"},
                {"id": "target-event", "text": "记住 启用缓存可以降低接口延迟。"},
            ],
            "discovery": {
                "from_event_id": "source-event",
                "to_event_id": "target-event",
                "type": "supports",
                "confidence": 0.95,
            },
        },
        "expected": {"relation_type": "supports"},
    }

    result = run_case(case, database_path)
    database = Database(database_path)
    try:
        connection = database.open()
        proposal = connection.execute("SELECT relation,status FROM relation_proposals").fetchone()
        official_relation_count = connection.execute("SELECT count(*) FROM memory_relations").fetchone()[0]
    finally:
        database.close()

    assert result["passed"] is True
    assert tuple(proposal) == ("supports", "pending")
    assert official_relation_count == 0


@pytest.mark.parametrize(
    ("schema_version", "hash_algorithm"),
    [
        (1, HASH_ALGORITHM),
        (BASELINE_SCHEMA_VERSION, "sha256-bytes-v0"),
        (BASELINE_SCHEMA_VERSION, None),
    ],
)
def test_compare_baseline_rejects_unknown_hash_contract(
    tmp_path,
    schema_version: int,
    hash_algorithm: str | None,
) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "schema_version": schema_version,
                "hash_algorithm": hash_algorithm,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="schema_version|hash_algorithm"):
        compare_baseline(baseline, "digest", {}, {})
