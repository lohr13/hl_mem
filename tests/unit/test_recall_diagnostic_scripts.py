"""召回与提取诊断脚本的行为测试。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import scripts.ab_test_index_text as ab_module
from hl_mem.domain.claims.claim import build_index_text
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from scripts.ab_test_index_text import (
    DiagnosticQuery,
    compare_index_text_modes,
    open_readonly_database,
    parse_args,
    run_ab_test,
)
from scripts.diagnose_extraction_gaps import KeywordDomain, diagnose_domains


def test_compare_index_text_modes_ranks_each_target() -> None:
    """四种模式都必须为诊断目标生成可比较的 dense 排名。"""
    claims = [
        {
            "id": "gpu",
            "subject_entity_id": "用户",
            "predicate": "使用",
            "value": "REDACTED_GPU 支持 CUDA",
            "canonical_slot": None,
            "topic_tags": ["hardware"],
        },
        {
            "id": "database",
            "subject_entity_id": "hl_mem",
            "predicate": "使用",
            "value": "SQLite WAL",
            "canonical_slot": "config.database",
            "topic_tags": ["dependency"],
        },
    ]
    query = DiagnosticQuery("GPU 硬件信息", ("REDACTED_GPU", "CUDA"))

    rows = compare_index_text_modes(claims, [query], FakeEmbedder(32))

    assert {(row.mode, row.target_claim_id) for row in rows} == {
        ("legacy", "gpu"),
        ("value_only", "gpu"),
        ("natural", "gpu"),
        ("answerable", "gpu"),
    }
    assert all(row.rank >= 1 for row in rows)


def test_ab_test_uses_isolated_copies_and_preserves_source_snapshot(tmp_path: Path) -> None:
    """端到端 A/B 必须从同一快照重建两个副本，且绝不写源文件。"""
    snapshot = tmp_path / "source.db"
    dataset = tmp_path / "gold.jsonl"
    settings = Settings(
        database_path=str(snapshot),
        embedder_mode="fake",
        embedding_dim=8,
        extractor_mode="fake",
        reranker_mode="off",
        query_expansion_mode="off",
        index_backfill_batch_size=10,
    )
    embedder = FakeEmbedder(8)
    database = Database(settings=settings)
    connection = database.open()
    claim = {
        "id": "c1",
        "namespace_key": "tenant-a",
        "subject_entity_id": "hl_mem",
        "predicate": "使用",
        "value": "SQLite WAL",
        "qualifiers": {},
        "fact_hash": "fact-c1",
        "conflict_key": None,
        "conflict_key_version": 3,
        "legacy_conflict_key": None,
        "valid_from": "2026-01-01T00:00:00+00:00",
        "recorded_from": "2026-01-01T00:00:00+00:00",
        "observed_at": "2026-01-01T00:00:00+00:00",
        "volatility": "stable",
        "status": "active",
        "confidence": 0.95,
        "importance": 0.8,
        "scope": "permanent",
        "access_count": 0,
        "source_authority": "medium",
        "extractor_version": "fake-v1",
        "embedding_model": embedder.model,
        "embedding_dim": embedder.dim,
        "embedding_dense": embedder.embed_one("SOURCE PROJECTION"),
        "canonical_attribute": "memory.explicit",
        "canonical_slot": "config.database",
        "topic_tags_json": '["dependency"]',
        "index_text": "SOURCE PROJECTION",
    }
    ClaimRepository(connection).insert_claim(claim)
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    database.close()
    dataset.write_text(
        json.dumps(
            {
                "id": "q1",
                "query": "hl_mem SQLite",
                "intent": "current_state",
                "namespace": "tenant-a",
                "expected_claim_ids": ["c1"],
                "equivalent_ids": [],
                "forbidden_ids": [],
                "slice": "exact_entity",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    source_before = snapshot.read_bytes()
    runner_calls: list[dict[str, object]] = []

    def fake_runner(
        mode_snapshot: Path,
        gold: Path,
        top_k: int,
        mode_settings: Settings | None,
    ) -> dict[str, object]:
        assert gold == dataset.resolve()
        assert top_k == 5
        assert mode_settings is not None
        mode = mode_settings.index_text_mode
        mode_connection = open_readonly_database(mode_snapshot)
        try:
            stored = mode_connection.execute(
                "SELECT index_text,embedding_model,embedding_dim,length(embedding_dense) " "FROM claims WHERE id='c1'"
            ).fetchone()
        finally:
            mode_connection.close()
        runner_calls.append(
            {
                "mode": mode,
                "index_text": stored[0],
                "model": stored[1],
                "dim": stored[2],
                "blob_length": stored[3],
            }
        )
        metrics = {
            "recall_at_5": 1.0,
            "mrr": 1.0 if mode == "legacy" else 1.02,
            "precision_at_3": 1.0 / 3.0,
            "no_answer_precision": 1.0,
            "no_answer_recall": 1.0,
        }
        return {
            "artifacts": {"dataset_sha256": "gold-sha256"},
            "metrics": metrics,
            "queries": [
                {
                    "id": "q1",
                    "returned_ids": ["c1"],
                    "answerability": "supported",
                }
            ],
        }

    report = run_ab_test(
        snapshot,
        dataset,
        5,
        settings,
        embedder=embedder,
        evaluation_runner=fake_runner,
    )

    assert snapshot.read_bytes() == source_before
    assert report["source_snapshot"]["sha256_before"] == report["source_snapshot"]["sha256_after"]
    assert report["experiment"]["differing_settings"] == ["index_text_mode"]
    assert report["canonical_digest"]["unchanged_in_each_arm"] is True
    assert {report["modes"][mode]["projection"]["canonical_digest"]["after"] for mode in ("legacy", "answerable")} == {
        report["canonical_digest"]["sha256"]
    }
    assert [call["mode"] for call in runner_calls] == ["legacy", "answerable"]
    projected_claim = {**claim, "topic_tags": ["dependency"]}
    assert runner_calls[0]["index_text"] == build_index_text(projected_claim, mode="legacy")
    assert runner_calls[1]["index_text"] == build_index_text(projected_claim, mode="answerable")
    assert all(call["model"] == "fake" for call in runner_calls)
    assert all(call["dim"] == 8 and call["blob_length"] == 32 for call in runner_calls)
    query = report["comparison"]["queries"][0]
    assert query["legacy"]["dense_rank"] == 1
    assert query["legacy"]["fts_rank"] == 1
    assert query["legacy"]["pipeline_rank"] == 1
    assert query["answerable"]["dense_rank"] == 1
    assert query["answerable"]["fts_rank"] == 1
    assert query["answerable"]["pipeline_rank"] == 1
    assert round(report["comparison"]["pipeline_metrics"]["mrr"]["delta"], 6) == 0.02

    real_pipeline = run_ab_test(snapshot, dataset, 5, settings, embedder=embedder)
    assert real_pipeline["modes"]["legacy"]["pipeline"]["case_count"] == 1
    assert real_pipeline["modes"]["answerable"]["pipeline"]["case_count"] == 1
    assert real_pipeline["evaluation_reference_time"]
    assert {
        real_pipeline["modes"][mode]["pipeline"]["config"]["reference_time"] for mode in ("legacy", "answerable")
    } == {real_pipeline["evaluation_reference_time"]}


def test_open_readonly_database_rejects_writes(tmp_path: Path) -> None:
    """诊断连接必须由 SQLite 强制只读。"""
    database_path = tmp_path / "readonly.db"
    sqlite3.connect(database_path).execute("CREATE TABLE sample(id INTEGER)").connection.close()

    connection = open_readonly_database(database_path)

    try:
        try:
            connection.execute("INSERT INTO sample VALUES (1)")
        except sqlite3.OperationalError as error:
            assert "readonly" in str(error).lower()
        else:
            raise AssertionError("read-only diagnostic connection accepted a write")
    finally:
        connection.close()


def test_ab_report_cannot_overwrite_snapshot_or_dataset(tmp_path: Path) -> None:
    """报告路径不得覆盖冻结快照或 gold 数据集。"""
    snapshot = tmp_path / "snapshot.db"
    dataset = tmp_path / "gold.jsonl"
    with pytest.raises(SystemExit):
        parse_args(["--snapshot", str(snapshot), "--dataset", str(dataset), "--report", str(snapshot)])
    with pytest.raises(SystemExit):
        parse_args(["--snapshot", str(snapshot), "--dataset", str(dataset), "--report", str(dataset)])


def test_legacy_diagnostic_cli_includes_answerable(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """旧 --diagnostic-set 路径继续可用，并将 answerable 纳入比较。"""
    snapshot = tmp_path / "diagnostic.db"
    database = Database(snapshot)
    connection = database.open()
    ClaimRepository(connection).insert_claim(
        {
            "id": "gpu",
            "namespace_key": "default",
            "subject_entity_id": "用户",
            "predicate": "使用",
            "value": "REDACTED_GPU 支持 CUDA",
            "status": "active",
            "recorded_from": "2026-01-01T00:00:00+00:00",
        }
    )
    database.close()
    diagnostics = tmp_path / "diagnostics.json"
    diagnostics.write_text(
        json.dumps(
            [
                {
                    "query": "GPU 硬件信息",
                    "target_terms": ["REDACTED_GPU", "CUDA"],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    settings = Settings(embedder_mode="fake", embedding_dim=8)
    monkeypatch.setattr(ab_module, "load_settings", lambda *_: settings)
    monkeypatch.setattr(ab_module, "make_embedder", lambda _: FakeEmbedder(8))

    assert (
        ab_module.main(
            [
                "--snapshot",
                str(snapshot),
                "--diagnostic-set",
                str(diagnostics),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "| answerable |" in output
    assert "| legacy |" in output


def test_diagnose_domains_reports_coverage_and_uncovered_samples() -> None:
    """覆盖诊断应按 evidence 关联识别命中但未形成对应 claim 的事件。"""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE events (
            id TEXT PRIMARY KEY,
            actor_type TEXT,
            session_id TEXT,
            event_type TEXT,
            content_json TEXT,
            recorded_at TEXT
        );
        CREATE TABLE claims (id TEXT PRIMARY KEY, value_json TEXT, subject_entity_id TEXT, predicate TEXT);
        CREATE TABLE evidence_links (
            derived_type TEXT,
            derived_id TEXT,
            evidence_type TEXT,
            evidence_id TEXT
        );
        """)
    connection.executemany(
        "INSERT INTO events VALUES (?,?,?,?,?,?)",
        [
            (
                "e1",
                "user",
                "s1",
                "message",
                json.dumps({"text": "我在做 lip-rt 唇形同步"}),
                "2026-07-27T00:00:00Z",
            ),
            (
                "e2",
                "user",
                "s1",
                "message",
                json.dumps({"text": "MuseTalk 也是候选"}),
                "2026-07-27T00:00:01Z",
            ),
        ],
    )
    connection.execute(
        "INSERT INTO claims VALUES (?,?,?,?)",
        ("c1", json.dumps("lip-rt 用于唇形同步"), "lip-rt", "事实"),
    )
    connection.execute("INSERT INTO evidence_links VALUES ('claim','c1','event','e1')")

    reports = diagnose_domains(
        connection,
        [KeywordDomain("lip-sync", ("lip-rt", "唇形同步", "MuseTalk"))],
        sample_limit=3,
    )

    report = reports[0]
    assert report.event_hits == 2
    assert report.claim_hits == 1
    assert report.coverage == 0.5
    assert [sample.event_id for sample in report.samples] == ["e2"]
    assert report.filter_reasons == {"eligible": 2}
