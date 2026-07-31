from __future__ import annotations

import json

import pytest

from scripts.run_quality_smoke import (
    BASELINE_SCHEMA_VERSION,
    HASH_ALGORITHM,
    compare_baseline,
    dataset_hash,
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
