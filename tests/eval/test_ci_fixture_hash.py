from __future__ import annotations

import json
from pathlib import Path

from tests.eval.dataset import load_cases
from tests.eval.eval_runner import _sha256_utf8_lf
from tests.eval.fixtures.build_ci_snapshot import _claim_specs, _fixture_sha256

PUBLIC = Path(__file__).parent / "public"
DATASET = PUBLIC / "recall_core_v1.jsonl"
BASELINE = PUBLIC / "recall_core_v1.baseline.json"


def test_ci_fixture_hash_is_stable_across_newline_styles(tmp_path: Path) -> None:
    canonical = DATASET.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    specs = _claim_specs(load_cases(DATASET))
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))

    assert baseline["status"] == "public_release_baseline"
    assert baseline["fixture_id"] == "core-recall-public-v1"

    dataset_hashes: set[str] = set()
    fixture_hashes: set[str] = set()
    for index, newline in enumerate(("\n", "\r\n", "\r")):
        dataset = tmp_path / f"recall-v2-{index}.jsonl"
        dataset.write_text(canonical.replace("\n", newline), encoding="utf-8", newline="")
        dataset_hashes.add(_sha256_utf8_lf(dataset))
        fixture_hashes.add(_fixture_sha256(dataset, specs, 2048))

    assert dataset_hashes == {baseline["dataset_sha256"]}
    assert fixture_hashes == {baseline["fixture_sha256"]}
