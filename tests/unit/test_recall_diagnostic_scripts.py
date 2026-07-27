"""召回与提取诊断脚本的行为测试。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from hl_mem.ingest.embedder import FakeEmbedder
from scripts.ab_test_index_text import (
    DiagnosticQuery,
    compare_index_text_modes,
    open_readonly_database,
)
from scripts.diagnose_extraction_gaps import KeywordDomain, diagnose_domains


def test_compare_index_text_modes_ranks_each_target() -> None:
    """三种模式都必须为诊断目标生成可比较的 dense 排名。"""
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
    }
    assert all(row.rank >= 1 for row in rows)


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
            ("e1", "user", "s1", "message", json.dumps({"text": "我在做 lip-rt 唇形同步"}), "2026-07-27T00:00:00Z"),
            ("e2", "user", "s1", "message", json.dumps({"text": "MuseTalk 也是候选"}), "2026-07-27T00:00:01Z"),
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
