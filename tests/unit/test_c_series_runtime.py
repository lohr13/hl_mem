"""C-series runner 必须走真实 RecallService 的回归测试。"""

from __future__ import annotations

import dataclasses
import hashlib
import json

from hl_mem.evaluation.c_series import completed_run_keys
from hl_mem.evaluation.c_series_runtime import (
    assert_gold_free,
    execute_planner_subgoals,
    execute_raw_rescue,
    materialize_visible_case,
    materialize_visible_case_cached,
    recall_visible_case,
)
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.recall.reranker import FakeReranker
from hl_mem.settings import Settings, VectorBackend


def _settings() -> Settings:
    return dataclasses.replace(
        Settings(),
        embedding_dim=8,
        vector_backend=VectorBackend.SQLITE_SCAN,
        query_expansion_mode="off",
        relation_discovery_mode="off",
        recall_dense_enabled=False,
        recall_candidate_floor=10,
        packed_context_token_budget=2_000,
    )


def _case() -> dict:
    return {
        "case_id": "runtime-two-hop",
        "namespace": "wanted",
        "question": "白鹭项目负责人的常驻城市是哪里？",
        "question_at": "2026-06-30T00:00:00+00:00",
        "known_as_of": "2026-06-30T00:00:00+00:00",
        "allowed_modalities": ["text"],
        "source_corpora": [{"id": "unit_fixture", "sha256": "f" * 64}],
        "events": [
            {"event_id": "e1", "text": "白鹭项目负责人是赵岚", "occurred_at": "2026-01-01T00:00:00+00:00"},
            {"event_id": "e2", "text": "赵岚常驻宁波", "occurred_at": "2026-01-02T00:00:00+00:00"},
        ],
        "claims": [
            {
                "claim_id": "seed",
                "text": "白鹭项目负责人是赵岚",
                "entities": ["白鹭项目", "赵岚"],
                "role": "白鹭项目",
                "action": "负责",
                "object": "赵岚",
                "rank": 1,
                "evidence_event_ids": ["e1"],
            },
            {
                "claim_id": "bridge",
                "text": "赵岚个人档案节点",
                "entities": ["赵岚"],
                "role": "赵岚",
                "action": "关联",
                "object": "个人档案",
                "rank": 20,
                "evidence_event_ids": ["e1"],
            },
            {
                "claim_id": "answer",
                "text": "赵岚常驻宁波",
                "entities": ["赵岚", "宁波"],
                "role": "赵岚",
                "action": "常驻",
                "object": "宁波",
                "rank": 21,
                "evidence_event_ids": ["e2"],
            },
            {
                "claim_id": "planner-answer",
                "text": "陆鸣是韩清的导师",
                "entities": ["陆鸣", "韩清"],
                "role": "韩清",
                "action": "导师是",
                "object": "陆鸣",
                "rank": 22,
                "evidence_event_ids": ["e2"],
            },
        ],
        "relations": [
            {"from_id": "seed", "to_id": "bridge", "relation": "about", "confidence": 1.0},
            {"from_id": "bridge", "to_id": "answer", "relation": "supports", "confidence": 1.0},
        ],
    }


def test_real_recall_service_separates_c0_c1_c3_and_preserves_source_hash(tmp_path) -> None:
    db_path = tmp_path / "dev.db"
    materialize_visible_case(db_path, _case(), _settings(), embedder=FakeEmbedder(8))
    before = db_path.read_bytes()
    common = (_case(), _settings(), FakeEmbedder(8), FakeReranker())
    c0 = recall_visible_case(*common, db_path=db_path, arm_id="C0")
    c1 = recall_visible_case(*common, db_path=db_path, arm_id="C1")
    c3 = recall_visible_case(*common, db_path=db_path, arm_id="C3")
    assert c0.packet[0]["claim_id"] == "seed"
    assert "bridge" in {item["claim_id"] for item in c1.packet}
    assert "answer" not in {item["claim_id"] for item in c1.packet}
    assert "answer" in {item["claim_id"] for item in c3.packet}
    assert c3.answerability in {"supported", "low_confidence"}
    assert c3.search_trace["candidates"]["answer"]["relation_paths"]
    assert [item["claim_id"] for item in c3.seed_packet] == ["seed"]
    assert all(item["evidence_provenance"] for item in c3.packet)
    provenance = c3.packet[0]["evidence_provenance"][0]
    assert provenance["namespace"] == "wanted"
    assert provenance["modality"] == "text"
    assert provenance["content_kind"] == "message"
    assert provenance["source_cache_identity"] == str(db_path.resolve())
    assert provenance["source_cache_sha256"] == hashlib.sha256(before).hexdigest()
    assert provenance["source_corpora"] == _case()["source_corpora"]
    assert db_path.read_bytes() == before


def test_visible_cache_reuses_frozen_sqlite_bytes(tmp_path) -> None:
    db_path = tmp_path / "cached.db"
    manifest_path = materialize_visible_case_cached(db_path, _case(), _settings())
    first_db = db_path.read_bytes()
    first_manifest = manifest_path.read_bytes()
    materialize_visible_case_cached(db_path, _case(), _settings())
    assert db_path.read_bytes() == first_db
    assert manifest_path.read_bytes() == first_manifest
    assert b'"contains_gold": false' in first_manifest


def test_c5_raw_rescue_uses_real_fts_visibility_and_replaces_tail(tmp_path) -> None:
    case = _case()
    case["events"].extend(
        {"event_id": f"extra-{index}", "text": f"负责人 城市 raw {index}", "occurred_at": "2026-01-03T00:00:00+00:00"}
        for index in range(12)
    )
    case["events"].append(
        {"event_id": "future", "text": "负责人 城市 future", "occurred_at": "2027-01-01T00:00:00+00:00"}
    )
    case["events"].append(
        {
            "event_id": "other-namespace",
            "tenant_id": "not-wanted",
            "text": "负责人 城市 secret",
            "occurred_at": "2026-01-01T00:00:00+00:00",
        }
    )
    db_path = tmp_path / "raw.db"
    materialize_visible_case(db_path, case, _settings(), embedder=FakeEmbedder(8))
    base = recall_visible_case(case, _settings(), FakeEmbedder(8), FakeReranker(), db_path=db_path, arm_id="C4")
    rescued = execute_raw_rescue(
        db_path,
        case,
        base,
        query="负责人 城市",
        settings=_settings(),
    )
    assert len(rescued) <= 10
    raw = [item for item in rescued if item.get("kind") == "raw_event"]
    assert 0 < len(raw) <= 6
    assert sum(int(item["token_count"]) for item in raw) <= 800
    assert all(item["event_id"] != "future" for item in raw)
    assert all(item["event_id"] != "other-namespace" for item in raw)
    assert all(item["evidence_provenance"][0]["event_id"] == item["event_id"] for item in raw)


def test_planner_subgoals_execute_real_recall_and_change_packet(tmp_path) -> None:
    case = _case()
    db_path = tmp_path / "planner.db"
    materialize_visible_case(db_path, case, _settings(), embedder=FakeEmbedder(8))
    base = recall_visible_case(case, _settings(), FakeEmbedder(8), FakeReranker(), db_path=db_path, arm_id="C4")
    assert "planner-answer" not in {item["claim_id"] for item in base.packet}
    assert [item["claim_id"] for item in base.seed_packet] == ["seed"]
    merged = execute_planner_subgoals(
        db_path,
        case,
        base,
        ({"query": "韩清 导师", "max_depth": 1},),
        settings=_settings(),
        embedder=FakeEmbedder(8),
        reranker=FakeReranker(),
    )
    assert "planner-answer" in {item["claim_id"] for item in merged}


def test_gold_free_audit_and_jsonl_truncated_tail(tmp_path) -> None:
    assert_gold_free({"question": "谁负责项目？", "namespace": "n"})
    for forbidden in ("gold", "answer", "forbidden_entities", "accepted_rubrics"):
        try:
            assert_gold_free({forbidden: "leak"})
        except ValueError:
            pass
        else:
            raise AssertionError(f"{forbidden} leak was accepted")
    path = tmp_path / "raw.jsonl"
    path.write_text(
        json.dumps({"status": "complete", "case_id": "one", "repeat_index": 0, "arm_id": "C0"})
        + "\n"
        + '{"status":"comp',
        encoding="utf-8",
    )
    assert completed_run_keys(path) == {("one", 0, "C0")}
    path.write_text("{broken}\n{}\n", encoding="utf-8")
    try:
        completed_run_keys(path)
    except ValueError:
        pass
    else:
        raise AssertionError("middle JSONL corruption was accepted")
