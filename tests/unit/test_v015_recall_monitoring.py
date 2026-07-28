"""v0.15.0 校准、索引文本与 provider 持久化测试。"""

import json

import pytest

from hl_mem.application.ingest import IngestService
from hl_mem.domain.claims.claim import build_index_text
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.monitoring.metrics import PersistentProviderMetrics, ProviderCall
from hl_mem.recall.calibration import fit_logistic
from hl_mem.storage.database import Database


def test_calibration_probability_orders_relevant_above_irrelevant() -> None:
    model = fit_logistic(
        [({"semantic": 0.9}, 1), ({"semantic": 0.1}, 0)], iterations=500
    )
    assert model.predict({"semantic": 0.9}) > model.predict({"semantic": 0.1})


def test_claim_index_text_contains_slot_and_tags() -> None:
    text = build_index_text(
        {
            "subject_entity_id": "hl_mem",
            "predicate": "embedding 模型",
            "value": "text-embedding-v4 2048维",
            "canonical_slot": "config.model",
            "topic_tags": ["model", "implementation"],
        }
    )
    assert (
        text
        == "hl_mem embedding 模型 text-embedding-v4 2048维 config.model model implementation"
    )


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("legacy", "hl_mem 使用 SQLite config.database database implementation"),
        ("value_only", "SQLite"),
        ("natural", "hl_mem：SQLite"),
    ],
)
def test_claim_index_text_supports_configured_modes(mode: str, expected: str) -> None:
    """索引文本模式必须只改变送入 embedding 的文本表示。"""
    claim = {
        "subject_entity_id": "hl_mem",
        "predicate": "使用",
        "value": "SQLite",
        "canonical_slot": "config.database",
        "topic_tags": ["database", "implementation"],
    }

    assert build_index_text(claim, mode=mode) == expected


def test_claim_index_text_rejects_unknown_mode() -> None:
    """拼写错误的模式不能静默退回 legacy。"""
    with pytest.raises(ValueError, match="unsupported index_text mode"):
        build_index_text({"value": "SQLite"}, mode="unknown")


def test_store_extracted_uses_selected_index_text_mode(tmp_path) -> None:
    """写入管线必须把所选格式同时用于 index_text 和 embedding。"""
    connection = Database(tmp_path / "index-mode.db").open()
    extracted = ExtractedClaim(subject="hl_mem", predicate="使用", value="SQLite")
    event = {
        "id": "event-index-mode",
        "tenant_id": "default",
        "actor_type": "user",
        "occurred_at": "2026-07-27T00:00:00+00:00",
    }

    result = IngestService.store_extracted(
        connection,
        extracted,
        event,
        "2026-07-27T00:00:00+00:00",
        FakeEmbedder(8),
        index_text_mode="value_only",
    )

    row = connection.execute(
        "SELECT index_text,value_json FROM claims WHERE id=?", (result.claim_id,)
    ).fetchone()
    assert row["index_text"] == "SQLite"
    assert json.loads(row["value_json"]) == "SQLite"


def test_provider_calls_persist_across_recorders(tmp_path) -> None:
    database = Database(tmp_path / "provider.db")
    connection = database.open()
    PersistentProviderMetrics(connection).record(
        ProviderCall("llm", "extract", "success", 12.0, query_id="q1")
    )
    row = connection.execute(
        "SELECT provider_type,query_id FROM provider_calls"
    ).fetchone()
    assert tuple(row) == ("llm", "q1")
    database.close()
