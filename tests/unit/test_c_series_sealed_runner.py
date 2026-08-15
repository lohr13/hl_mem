"""C-series sealed 2x2 runner contracts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
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
        "relation_coverage": "required",
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


def _relation_db(path, relations: int) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE memory_relations(id TEXT PRIMARY KEY)")
        connection.executemany(
            "INSERT INTO memory_relations(id) VALUES (?)",
            [(f"relation-{index}",) for index in range(relations)],
        )
        connection.commit()
    finally:
        connection.close()


def test_relation_coverage_gate_rejects_required_case_without_edges(tmp_path) -> None:
    database = tmp_path / "required.db"
    _relation_db(database, 0)

    with pytest.raises(RuntimeError, match="required.*0"):
        runner.validate_relation_coverage(
            [{"case_id": "case-required", "relation_coverage": "required"}],
            {"case-required": database},
        )


def test_relation_coverage_gate_rejects_none_case_with_edges(tmp_path) -> None:
    database = tmp_path / "none.db"
    _relation_db(database, 1)

    with pytest.raises(RuntimeError, match="none.*1"):
        runner.validate_relation_coverage(
            [{"case_id": "case-none", "relation_coverage": "none"}],
            {"case-none": database},
        )


def test_relation_coverage_gate_reports_required_and_none_separately(tmp_path) -> None:
    required = tmp_path / "required.db"
    none = tmp_path / "none.db"
    _relation_db(required, 2)
    _relation_db(none, 0)

    summary = runner.validate_relation_coverage(
        [
            {"case_id": "case-required", "relation_coverage": "required"},
            {"case_id": "case-none", "relation_coverage": "none"},
        ],
        {"case-required": required, "case-none": none},
    )

    assert summary == {
        "required_cases": 1,
        "required_with_edges": 1,
        "none_cases": 1,
        "none_with_edges": 0,
        "total_relations": 2,
        "by_case": {
            "case-none": {"declared": "none", "relations": 0},
            "case-required": {"declared": "required", "relations": 2},
        },
    }


def _smoke_packets(*, equal_case: str | None = None) -> dict:
    packets = []
    for case_id in ("case-1", "case-2", "case-3"):
        c0 = [{"claim_id": f"{case_id}-seed"}]
        c4 = list(c0) if case_id == equal_case else [*c0, {"claim_id": f"{case_id}-expanded"}]
        packets.extend(
            [
                {"packet_key": f"{case_id}|0|C0", "case_id": case_id, "repeat_index": 0, "arm_id": "C0", "packet": c0},
                {"packet_key": f"{case_id}|0|C4", "case_id": case_id, "repeat_index": 0, "arm_id": "C4", "packet": c4},
            ]
        )
    return {"packets": packets}


def test_packet_smoke_rejects_any_sampled_c0_c4_equal_pair() -> None:
    with pytest.raises(RuntimeError, match="packet smoke.*case-2"):
        runner.assert_c0_c4_packet_smoke(
            _smoke_packets(equal_case="case-2"),
            ["case-1", "case-2", "case-3"],
            "sealed-v2",
        )


def test_packet_smoke_allows_three_distinct_c4_packets() -> None:
    summary = runner.assert_c0_c4_packet_smoke(
        _smoke_packets(),
        ["case-1", "case-2", "case-3"],
        "sealed-v2",
    )

    assert summary["passed"] is True
    assert sorted(summary["case_ids"]) == ["case-1", "case-2", "case-3"]
    assert summary["equal_pairs"] == []


def test_v2_suite_paths_are_isolated_from_v1() -> None:
    v1 = runner.suite_paths("v1")
    v2 = runner.suite_paths("v2")

    assert v1.raw.name == "c_series_sealed_raw.jsonl"
    assert v2.raw.name == "c_series_sealed_raw_v2.jsonl"
    assert v2.cache_root.name == "c_series_sealed_cache_v2"
    assert v1.holdout_manifest != v2.holdout_manifest
    assert all("v2" in path.name for path in (v2.cache_root, v2.prereg, v2.inputs, v2.packets, v2.raw, v2.report))


def test_v2_preregistration_cannot_omit_or_downgrade_suite_binding() -> None:
    with pytest.raises(ValueError, match="suite_version"):
        runner._assert_suite_binding({}, "v2")

    with pytest.raises(ValueError, match="suite.*mismatch"):
        runner._assert_suite_binding({"suite_version": "v1"}, "v2")


def test_legacy_v1_preregistration_remains_bound_to_v1() -> None:
    assert runner._assert_suite_binding({}, "v1") == "v1"
    assert runner._assert_suite_binding({"suite_version": "v1"}, "v1") == "v1"


def test_cli_binds_selected_suite_before_dispatch(monkeypatch) -> None:
    selected = []

    def fake_dry_run() -> int:
        selected.append((runner.CURRENT_SUITE.version, runner.HOLDOUT_MANIFEST.name))
        return 0

    monkeypatch.setattr(runner, "command_dry_run", fake_dry_run)

    assert runner.main(["--suite", "v2", "dry-run"]) == 0
    assert selected == [("v2", "relation_chain_holdout_v2_manifest.json")]


def test_v1_prepare_cache_does_not_apply_v2_relation_gate(monkeypatch) -> None:
    runner.configure_suite("v1")
    monkeypatch.setattr(runner, "load_settings", lambda *_args: SimpleNamespace(llm_base_url="coding", llm_api_key="k"))
    monkeypatch.setattr(runner, "_safe_holdout_cases", lambda: ([], runner.ROOT, "a" * 64))
    monkeypatch.setattr(runner, "initialize_process", lambda _settings: None)
    monkeypatch.setattr(runner, "make_embedder", lambda _settings: object())
    monkeypatch.setattr(runner, "_cache_config", lambda _settings: {})
    monkeypatch.setattr(
        runner,
        "validate_relation_coverage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("v2-only gate called")),
    )

    assert runner.command_prepare_cache() == 0
