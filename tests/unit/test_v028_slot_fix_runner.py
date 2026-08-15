from __future__ import annotations

import hashlib
import importlib
import json

import pytest

runner = importlib.import_module("evaluation.tools.run_v028_slot_fix")


def _hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _snapshot() -> dict[str, object]:
    cases = [
        {
            "case_id": "known-mispair",
            "slice": "known_mispair",
            "subject": "用户",
            "value": "用户决定使用 coding plan 的 qwen3.7-plus 模型",
            "kind": "choice",
            "canonical_attribute": "choice.model",
            "qualifiers": {"task": "用户"},
            "evidence": "用户决定使用 coding plan 的 qwen3.7-plus 模型",
            "old": {"canonical_slot": "choice.model", "required_qualifiers": {"task": "用户"}},
            "expected": {"canonical_slot": None, "required_qualifiers": {}},
        },
        {
            "case_id": "correct-anchor",
            "slice": "correct_anchor",
            "subject": "青岚项目",
            "value": "青岚项目采用 PostgreSQL 数据库",
            "kind": "choice",
            "canonical_attribute": "choice.database",
            "qualifiers": {"project": "青岚项目"},
            "evidence": "青岚项目采用 PostgreSQL 数据库",
            "old": {
                "canonical_slot": "choice.database",
                "required_qualifiers": {"project": "青岚项目"},
            },
            "expected": {
                "canonical_slot": "choice.database",
                "required_qualifiers": {"project": "青岚项目"},
            },
        },
    ]
    return {
        "schema_version": 1,
        "snapshot_id": "test-v027-slot-inputs",
        "source_contract": {"contract_id": "compact-7field-v1", "new_prompt_cache_used": False},
        "cases_sha256": _hash(cases),
        "cases": cases,
    }


def test_slot_fix_runner_rejects_snapshot_hash_mismatch(tmp_path) -> None:
    snapshot = _snapshot()
    snapshot["cases_sha256"] = "0" * 64
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="cases_sha256"):
        runner.load_snapshot(path)


def test_slot_fix_runner_rejects_new_prompt_cache_inputs(tmp_path) -> None:
    snapshot = _snapshot()
    snapshot["source_contract"]["new_prompt_cache_used"] = True
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="new prompt cache"):
        runner.load_snapshot(path)


def test_slot_fix_runner_scores_old_and_new_on_same_frozen_cases(tmp_path) -> None:
    snapshot = _snapshot()
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

    report = runner.score_snapshot(runner.load_snapshot(path))

    assert report["case_count"] == 2
    assert report["slices"] == {"correct_anchor": 1, "known_mispair": 1}
    assert report["old"] == {"mismatches": 1, "mismatch_rate": 0.5}
    assert report["new"] == {"mismatches": 0, "mismatch_rate": 0.0}
    assert report["paired"] == {"fixed": 1, "regressed": 0, "unchanged_wrong": 0, "unchanged_correct": 1}
