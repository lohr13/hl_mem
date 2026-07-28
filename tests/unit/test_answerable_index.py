"""answerable 索引文本与安全回填测试。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from hl_mem.domain.claims.claim import build_index_text
from hl_mem.errors import ConfigurationError
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.workers.backfill_index_text import backfill_index_text


@pytest.mark.parametrize(
    ("slot", "value", "expected"),
    [
        ("config.port", "8200", "hl_mem 服务端口 8200"),
        ("choice.database", "SQLite", "hl_mem 使用的数据库 SQLite"),
        ("identity.name", "小马", "user 用户名称 小马"),
    ],
)
def test_answerable_registered_slot(slot: str, value: str, expected: str) -> None:
    """注册 slot 使用 registry 中的可读描述。"""
    claim = {"subject_entity_id": "hl_mem", "predicate": "使用", "value": value, "canonical_slot": slot}
    if slot == "identity.name":
        claim["subject_entity_id"] = "user"
    assert build_index_text(claim, mode="answerable") == expected


def test_answerable_unknown_slot_falls_back_to_predicate() -> None:
    """未注册 slot 降级为 subject、predicate 与 value。"""
    claim = {"subject_entity_id": "hl_mem", "predicate": "部署于", "value": "本机", "canonical_slot": "custom.host"}
    assert build_index_text(claim, mode="answerable") == "hl_mem 部署于 本机"


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("legacy", "hl_mem 使用 SQLite choice.database database implementation"),
        ("value_only", "SQLite"),
        ("natural", "hl_mem：SQLite"),
    ],
)
def test_existing_modes_remain_compatible(mode: str, expected: str) -> None:
    """三个既有模式保持原输出。"""
    claim = {
        "subject_entity_id": "hl_mem",
        "predicate": "使用",
        "value": "SQLite",
        "canonical_slot": "choice.database",
        "topic_tags": ["database", "implementation"],
    }
    assert build_index_text(claim, mode=mode) == expected


def _insert_claim(connection, claim_id: str = "claim-1") -> None:
    ClaimRepository(connection).insert_claim(
        {
            "id": claim_id,
            "namespace_key": "default",
            "subject_entity_id": "hl_mem",
            "predicate": "使用",
            "value": "SQLite",
            "canonical_slot": "choice.database",
            "topic_tags_json": '["database"]',
            "status": "active",
            "confidence": 1.0,
            "importance": 0.5,
            "scope": "permanent",
            "valid_from": "2026-07-29T00:00:00+00:00",
            "recorded_from": "2026-07-29T00:00:00+00:00",
        }
    )


def test_backfill_dry_run_performs_zero_writes(tmp_path) -> None:
    """dry-run 只统计，不修改索引或 embedding。"""
    connection = Database(tmp_path / "dry-run.db").open()
    _insert_claim(connection)
    before = tuple(connection.execute("SELECT index_text,embedding_dense FROM claims").fetchone())
    result = backfill_index_text(
        connection, FakeEmbedder(8), mode="answerable", version="v1", batch_size=100, max_attempts=3, dry_run=True
    )
    after = tuple(connection.execute("SELECT index_text,embedding_dense FROM claims").fetchone())
    assert result.backfilled == 0
    assert result.provider_items == 0
    assert after == before


def test_backfill_is_idempotent(tmp_path) -> None:
    """重复回填跳过已使用目标文本的 claim。"""
    connection = Database(tmp_path / "idempotent.db").open()
    _insert_claim(connection)
    first = backfill_index_text(
        connection, FakeEmbedder(8), mode="answerable", version="v1", batch_size=100, max_attempts=3
    )
    second = backfill_index_text(
        connection, FakeEmbedder(8), mode="answerable", version="v1", batch_size=100, max_attempts=3
    )
    assert first.backfilled == 1
    assert second.backfilled == 0
    assert second.skipped == 1


def test_index_backfill_settings_validation() -> None:
    """新增回填参数必须为正数且版本非空。"""
    replace(Settings(), index_text_mode="answerable").validate()
    with pytest.raises(ConfigurationError):
        replace(Settings(), index_backfill_batch_size=0).validate()
    with pytest.raises(ConfigurationError):
        replace(Settings(), index_backfill_max_attempts=0).validate()
    with pytest.raises(ConfigurationError):
        replace(Settings(), index_text_version=" ").validate()
