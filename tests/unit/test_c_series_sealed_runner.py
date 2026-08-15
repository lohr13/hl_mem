"""C-series sealed 2x2 runner contracts."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from evaluation.tools import run_c_series_sealed_experiment as runner


def _manifest() -> dict:
    manifest = {
        "preregistration_id": "sealed-v1",
        "corpora": {"sealed_holdout": "a" * 64},
        "repeats": 3,
    }
    manifest["corpus_seed_sha256"] = runner._canonical_hash(manifest["corpora"])
    return manifest


def _inputs() -> dict:
    return {
        "cases": [
            {
                "case_id": "case-001",
                "question": "哪一项成立？",
                "namespace": "sealed-001",
                "question_at": "2026-01-01T00:00:00+00:00",
                "db_path": "case.db",
            }
        ]
    }


def test_tasks_cover_two_arms_two_readers_three_repeats_deterministically() -> None:
    first = runner.build_tasks(_manifest(), _inputs())
    second = runner.build_tasks(_manifest(), _inputs())

    assert first == second
    assert len(first) == 12
    assert {(repeat, arm, reader) for _, repeat, arm, reader in first} == {
        (repeat, arm, reader) for repeat in range(3) for arm in ("C0", "C4") for reader in ("qwen", "glm")
    }


def test_completed_keys_include_reader_dimension() -> None:
    rows = [
        {"status": "complete", "case_id": "case-001", "repeat_index": 0, "arm_id": "C4", "reader_id": "qwen"},
        {"status": "complete", "case_id": "case-001", "repeat_index": 0, "arm_id": "C4", "reader_id": "glm"},
        {"status": "retryable_error", "case_id": "case-001", "repeat_index": 0, "arm_id": "C0", "reader_id": "glm"},
    ]

    assert runner.completed_matrix_keys(rows) == {
        ("case-001", 0, "C4", "qwen"),
        ("case-001", 0, "C4", "glm"),
    }


def test_reader_snapshot_never_serializes_api_keys() -> None:
    qwen = SimpleNamespace(
        llm_provider="dashscope",
        llm_base_url="https://coding.dashscope.aliyuncs.com/v1",
        llm_model="qwen3.7-plus",
        llm_timeout=90.0,
    )

    snapshot = runner.reader_snapshot(qwen)

    assert snapshot == {
        "qwen": {
            "provider": "dashscope",
            "base_url": "https://coding.dashscope.aliyuncs.com/v1",
            "model": "qwen3.7-plus",
            "revision": "qwen3.7-plus",
            "endpoint_class": "coding-plan",
            "temperature": 0.1,
            "max_output_tokens": 512,
            "timeout_seconds": 90.0,
            "seed_support": "unsupported",
        },
        "glm": {
            "provider": "zhipu",
            "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
            "model": "glm-5.3",
            "revision": "glm-5.3",
            "endpoint_class": "coding-plan",
            "temperature": 0.1,
            "max_output_tokens": 512,
            "timeout_seconds": 90.0,
            "seed_support": "unsupported",
        },
    }
    assert "key" not in repr(snapshot).casefold()


def test_gold_free_case_uses_events_but_omits_answer_and_gold() -> None:
    raw = {
        "case_id": "case-001",
        "category": "cross_event_two_hop",
        "namespace": "sealed-001",
        "events": [{"event_id": "e1", "occurred_at": "2026-01-01T00:00:00+00:00", "text": "事件"}],
        "question_at": "2026-01-02T00:00:00+00:00",
        "question": "问题",
        "answer": "秘密答案",
        "gold": {"answer_entities": ["秘密答案"]},
        "provenance": "new",
    }

    safe = runner.gold_free_case(raw)

    assert safe == {
        "case_id": "case-001",
        "category": "cross_event_two_hop",
        "namespace": "sealed-001",
        "events": raw["events"],
        "question_at": "2026-01-02T00:00:00+00:00",
        "known_as_of": None,
        "question": "问题",
    }
    assert "秘密答案" not in repr(safe)


def test_public_snapshot_rejects_holdout_manifest_drift(tmp_path, monkeypatch) -> None:
    holdout_manifest = tmp_path / "holdout-manifest.json"
    holdout_manifest.write_text("frozen", encoding="utf-8")
    monkeypatch.setattr(runner, "HOLDOUT_MANIFEST", holdout_manifest)
    manifest = {
        "corpora": {
            "sealed_holdout_manifest": hashlib.sha256(b"frozen").hexdigest(),
        }
    }

    runner.verify_public_snapshot_files(manifest)
    holdout_manifest.write_text("drifted", encoding="utf-8")

    with pytest.raises(RuntimeError, match="holdout manifest drift"):
        runner.verify_public_snapshot_files(manifest)


def test_preregistration_rejects_old_incomplete_sealed_contract() -> None:
    old_contract = {
        "preregistration_id": "sealed-v1",
        "protocol_version": runner.PROTOCOL_VERSION,
        "scorer_version": "answer-entity-packet-v1",
        "git_commit": "a" * 40,
        "sealed_payload_sha256": "b" * 64,
        "arms": ["C0", "C4"],
        "readers": ["qwen", "glm"],
        "repeats": 3,
        "corpora": {"sealed_holdout": "b" * 64},
        "cache_files": {"case.db": "c" * 64},
        "inputs_sha256": "d" * 64,
        "packets_sha256": "e" * 64,
        "implementation_snapshot": {"version": "old"},
        "prompt_hashes": {"qa": "f" * 64},
        "runtime_config_sha256": "1" * 64,
        "authorization_override": {"authorized": True},
    }

    with pytest.raises(ValueError, match="runtime|snapshot_files|frozen_rules"):
        runner._validate_preregistration(old_contract)


def test_task_order_rejects_corpus_seed_drift() -> None:
    manifest = _manifest()
    manifest["corpus_seed_sha256"] = "0" * 64

    with pytest.raises(ValueError, match="corpus seed"):
        runner.build_tasks(manifest, _inputs())


def test_repair_jsonl_tail_truncates_partial_record_before_resume(tmp_path) -> None:
    path = tmp_path / "raw.jsonl"
    first = {"status": "complete", "case_id": "case-001"}
    path.write_bytes((json.dumps(first) + "\n" + '{"status":"com').encode())

    assert runner.repair_jsonl_tail(path) is True
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"status": "complete", "case_id": "case-002"}) + "\n")

    assert [row["case_id"] for row in runner._read_jsonl(path)] == ["case-001", "case-002"]


def test_repair_jsonl_tail_preserves_valid_record_without_newline(tmp_path) -> None:
    path = tmp_path / "raw.jsonl"
    path.write_text(json.dumps({"status": "complete", "case_id": "case-001"}), encoding="utf-8")

    assert runner.repair_jsonl_tail(path) is True
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"status": "complete", "case_id": "case-002"}) + "\n")

    assert [row["case_id"] for row in runner._read_jsonl(path)] == ["case-001", "case-002"]


def test_resume_rejects_rows_from_another_preregistration() -> None:
    manifest = {
        "preregistration_id": "sealed-v1",
        "models": {"readers": {"glm": {"model": "glm-5.3"}}},
    }
    rows = [
        {
            "status": "complete",
            "reader_id": "glm",
            "preregistration_id": "older-run",
            "preregistration_sha256": "a" * 64,
            "reader_snapshot_sha256": "b" * 64,
        }
    ]

    with pytest.raises(RuntimeError, match="different preregistration"):
        runner.verify_resume_bindings(rows, manifest, "a" * 64)


def test_e2e_cost_includes_frozen_recall_and_reader_wall_time() -> None:
    assert runner._e2e_latency(0.25, 1.75) == 2.0


def test_design_dev_catalog_freezes_case_ids_and_category_distribution() -> None:
    snapshot = runner.freeze_case_catalog(
        [
            {"case_id": "e2e-1", "category": "perltqa_relation", "dataset": "chinese_e2e"},
            {"case_id": "dev-1", "category": "cross_event_two_hop", "dataset": "relation_design_dev"},
        ]
    )

    assert snapshot == {
        "case_count": 2,
        "case_ids": ["dev-1", "e2e-1"],
        "category_distribution": {"cross_event_two_hop": 1, "perltqa_relation": 1},
        "dataset_distribution": {"chinese_e2e": 1, "relation_design_dev": 1},
    }


def test_packet_fingerprint_is_bound_to_preregistration_and_corpus_seed(monkeypatch) -> None:
    monkeypatch.setattr(runner.base, "_runtime_fingerprint", lambda settings: "runtime")
    first = runner._packet_fingerprint({}, object(), {}, "sealed-v1", "a" * 64)
    second = runner._packet_fingerprint({}, object(), {}, "sealed-v2", "b" * 64)

    assert first != second
