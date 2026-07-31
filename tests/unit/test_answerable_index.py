"""answerable 索引文本与安全回填测试。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import httpx
import pytest

import hl_mem.cli as cli_module
import hl_mem.workers.backfill_index_text as backfill_module
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


def _insert_claim(
    connection,
    claim_id: str = "claim-1",
    *,
    status: str = "active",
    qualifiers: dict[str, str] | None = None,
) -> None:
    claim = {
        "id": claim_id,
        "namespace_key": "default",
        "subject_entity_id": "hl_mem",
        "predicate": "使用",
        "value": "SQLite",
        "canonical_slot": "choice.database",
        "topic_tags_json": '["database"]',
        "status": status,
        "confidence": 1.0,
        "importance": 0.5,
        "scope": "permanent",
        "valid_from": "2026-07-29T00:00:00+00:00",
        "recorded_from": "2026-07-29T00:00:00+00:00",
    }
    if qualifiers is not None:
        claim["qualifiers"] = qualifiers
    ClaimRepository(connection).insert_claim(claim)


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
    assert result.would_update == 1
    assert result.skip == 0
    assert result.provider_items == 0
    assert result.estimated_provider_items == 1
    assert result.estimated_provider_requests == 1
    assert result.model == "fake"
    assert result.dim == 8
    assert result.cursor == "claim-1"
    assert len(result.text_hash) == 64
    assert result.coverage_complete
    assert {
        "would_update",
        "skip",
        "cursor",
        "text_hash",
        "provider_items",
        "provider_requests",
        "model",
        "dim",
    } <= result.to_dict().keys()
    assert after == before


def test_backfill_covers_recallable_statuses_and_ignores_candidate(tmp_path) -> None:
    """active/superseded/expired 均回填，candidate 不进入 eligible 扫描。"""
    connection = Database(tmp_path / "statuses.db").open()
    for status in ("active", "superseded", "expired", "candidate"):
        _insert_claim(connection, f"claim-{status}", status=status)
    candidate_before = connection.execute(
        "SELECT index_text,embedding_dense FROM claims WHERE id='claim-candidate'"
    ).fetchone()

    result = backfill_index_text(
        connection,
        FakeEmbedder(8),
        mode="answerable",
        version="v1",
        batch_size=2,
        max_attempts=1,
    )

    updated = connection.execute(
        "SELECT status,index_text,embedding_dense FROM claims "
        "WHERE status IN ('active','superseded','expired') ORDER BY status"
    ).fetchall()
    candidate_after = connection.execute(
        "SELECT index_text,embedding_dense FROM claims WHERE id='claim-candidate'"
    ).fetchone()
    assert result.scanned == 3
    assert result.backfilled == 3
    assert result.failed == 0
    assert result.coverage_complete
    assert all(row["index_text"] == "hl_mem 使用的数据库 SQLite" for row in updated)
    assert all(len(row["embedding_dense"]) == 32 for row in updated)
    assert tuple(candidate_after) == tuple(candidate_before)


def test_backfill_projection_includes_qualifiers(tmp_path) -> None:
    """回填投影必须解码 answerable slot 所需的 qualifiers。"""
    connection = Database(tmp_path / "qualifiers.db").open()
    _insert_claim(connection, qualifiers={"project": "HL-Mem"})

    result = backfill_index_text(
        connection,
        FakeEmbedder(8),
        mode="answerable",
        version="v1",
        batch_size=100,
        max_attempts=1,
    )

    assert result.backfilled == 1
    assert (
        connection.execute("SELECT index_text FROM claims WHERE id='claim-1'").fetchone()[0]
        == "hl_mem 使用的数据库 project: HL-Mem SQLite"
    )


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


def test_backfill_reembeds_when_model_or_dimension_changes(tmp_path) -> None:
    """索引文本未变时，模型或维度变化仍触发重新 embedding。"""
    connection = Database(tmp_path / "model-change.db").open()
    _insert_claim(connection)
    backfill_index_text(connection, FakeEmbedder(8), mode="answerable", version="v1", batch_size=100, max_attempts=1)

    changed = FakeEmbedder(16)
    changed.model = "fake-v2"
    result = backfill_index_text(connection, changed, mode="answerable", version="v2", batch_size=100, max_attempts=1)
    row = connection.execute("SELECT embedding_model,embedding_dim FROM claims WHERE id='claim-1'").fetchone()

    assert result.backfilled == 1
    assert result.model_version_reembedded == 1
    assert tuple(row) == ("fake-v2", 16)


def test_backfill_cas_rejects_concurrent_model_change(tmp_path) -> None:
    """embedding 期间模型字段被并发修改时，CAS 不得覆盖新值。"""
    connection = Database(tmp_path / "model-cas.db").open()
    _insert_claim(connection)

    class ConcurrentModelEmbedder(FakeEmbedder):
        """在 provider 调用期间模拟并发模型更新。"""

        def embed_batch(self, texts: list[str]) -> list[bytes]:
            connection.execute("UPDATE claims SET embedding_model='concurrent-model' WHERE id='claim-1'")
            connection.commit()
            return super().embed_batch(texts)

    result = backfill_index_text(
        connection,
        ConcurrentModelEmbedder(8),
        mode="answerable",
        version="v1",
        batch_size=100,
        max_attempts=1,
    )

    assert result.backfilled == 0
    assert result.skipped == 0
    assert result.failed == 1
    assert result.last_error_class == "CompareAndSetMismatch"
    assert not result.coverage_complete
    assert (
        connection.execute("SELECT embedding_model FROM claims WHERE id='claim-1'").fetchone()[0] == "concurrent-model"
    )


def test_backfill_retries_only_recoverable_errors_with_backoff(tmp_path, monkeypatch) -> None:
    """可恢复 provider 错误按 attempt 退避重试，并记录最后异常分类。"""
    connection = Database(tmp_path / "retry.db").open()
    _insert_claim(connection)
    sleeps: list[int] = []

    class FlakyEmbedder(FakeEmbedder):
        """首次超时、随后成功的测试 embedder。"""

        def __init__(self) -> None:
            super().__init__(8)
            self.calls = 0

        def embed_batch(self, texts: list[str]) -> list[bytes]:
            self.calls += 1
            if self.calls == 1:
                raise httpx.ReadTimeout("temporary timeout")
            return super().embed_batch(texts)

    embedder = FlakyEmbedder()
    monkeypatch.setattr(backfill_module.time, "sleep", sleeps.append)
    result = backfill_index_text(connection, embedder, mode="answerable", version="v1", batch_size=100, max_attempts=3)

    assert result.backfilled == 1
    assert result.last_error_class == "http_timeout"
    assert embedder.calls == 2
    assert sleeps == [2]


def test_backfill_does_not_retry_nonrecoverable_error(tmp_path, monkeypatch) -> None:
    """非可恢复异常直接失败且不执行退避。"""
    connection = Database(tmp_path / "no-retry.db").open()
    _insert_claim(connection)
    sleeps: list[int] = []

    class InvalidEmbedder(FakeEmbedder):
        """始终抛出输入错误的测试 embedder。"""

        def __init__(self) -> None:
            super().__init__(8)
            self.calls = 0

        def embed_batch(self, texts: list[str]) -> list[bytes]:
            self.calls += 1
            raise ValueError("invalid embedding input")

    embedder = InvalidEmbedder()
    monkeypatch.setattr(backfill_module.time, "sleep", sleeps.append)
    result = backfill_index_text(connection, embedder, mode="answerable", version="v1", batch_size=100, max_attempts=3)

    assert result.failed == 1
    assert result.last_error_class == "ValueError"
    assert embedder.calls == 1
    assert sleeps == []
    assert not result.coverage_complete


def test_backfill_rejects_provider_embedding_with_wrong_dimension(tmp_path) -> None:
    """provider 返回错误长度的向量时不写库并报告覆盖不完整。"""
    connection = Database(tmp_path / "wrong-dim.db").open()
    _insert_claim(connection)

    class WrongDimensionEmbedder(FakeEmbedder):
        def embed_batch(self, texts: list[str]) -> list[bytes]:
            return [b"\0" for _ in texts]

    result = backfill_index_text(
        connection,
        WrongDimensionEmbedder(8),
        mode="answerable",
        version="v1",
        batch_size=100,
        max_attempts=1,
    )

    row = connection.execute(
        "SELECT embedding_dense,embedding_model,embedding_dim FROM claims WHERE id='claim-1'"
    ).fetchone()
    assert result.failed == 1
    assert result.last_error_class == "EmbeddingDimensionMismatch"
    assert not result.coverage_complete
    assert tuple(row) == (None, None, None)


def test_backfill_failure_cursor_retries_failed_batch(tmp_path) -> None:
    """失败批次不得推进续跑 cursor，重试必须重新覆盖失败行。"""
    connection = Database(tmp_path / "retry-cursor.db").open()
    for claim_id in ("a", "b", "c"):
        _insert_claim(connection, claim_id)

    class SecondBatchFails(FakeEmbedder):
        def __init__(self) -> None:
            super().__init__(8)
            self.calls = 0

        def embed_batch(self, texts: list[str]) -> list[bytes]:
            self.calls += 1
            if self.calls == 2:
                raise ValueError("second batch failed")
            return super().embed_batch(texts)

    embedder = SecondBatchFails()
    failed = backfill_index_text(
        connection,
        embedder,
        mode="answerable",
        version="v1",
        batch_size=2,
        max_attempts=1,
    )

    assert failed.failed == 1
    assert failed.backfilled == 2
    assert failed.cursor == "b"
    assert not failed.coverage_complete

    resumed = backfill_index_text(
        connection,
        embedder,
        mode="answerable",
        version="v1",
        batch_size=2,
        max_attempts=1,
        cursor=failed.cursor,
    )
    assert resumed.failed == 0
    assert resumed.backfilled == 1
    assert resumed.skipped == 0
    assert resumed.coverage_complete


def test_backfill_apply_validates_global_coverage_before_success(tmp_path) -> None:
    """尾部 cursor 不能掩盖游标之前仍未回填的可召回 Claim。"""
    connection = Database(tmp_path / "global-coverage.db").open()
    _insert_claim(connection, "a")
    _insert_claim(connection, "b")

    partial = backfill_index_text(
        connection,
        FakeEmbedder(8),
        mode="answerable",
        version="v1",
        batch_size=10,
        max_attempts=1,
        cursor="a",
    )

    assert partial.scanned == 1
    assert partial.failed == 0
    assert not partial.coverage_complete
    assert partial.integrity_ok is False
    assert partial.cursor is None

    completed = backfill_index_text(
        connection,
        FakeEmbedder(8),
        mode="answerable",
        version="v1",
        batch_size=10,
        max_attempts=1,
        cursor=partial.cursor,
    )
    assert completed.backfilled == 1
    assert completed.skipped == 1
    assert completed.coverage_complete
    assert completed.integrity_ok is True


@pytest.mark.parametrize("invalid_embedding", ["x" * 32, 7])
def test_backfill_replaces_non_blob_embedding(tmp_path, invalid_embedding) -> None:
    """等长 TEXT 或 INTEGER 都不能冒充 float32 embedding BLOB。"""
    connection = Database(tmp_path / f"non-blob-{type(invalid_embedding).__name__}.db").open()
    _insert_claim(connection)
    target = "hl_mem 使用的数据库 SQLite"
    connection.execute(
        "UPDATE claims SET index_text=?,embedding_dense=?,embedding_model='fake',embedding_dim=8",
        (target, invalid_embedding),
    )
    connection.commit()

    result = backfill_index_text(
        connection,
        FakeEmbedder(8),
        mode="answerable",
        version="v1",
        batch_size=10,
        max_attempts=1,
    )

    stored = connection.execute("SELECT embedding_dense FROM claims").fetchone()[0]
    assert result.backfilled == 1
    assert result.coverage_complete
    assert isinstance(stored, bytes)
    assert len(stored) == 32


def test_backfill_apply_fails_fts_integrity_validation(tmp_path) -> None:
    """投影写入未同步 FTS 时必须报告覆盖不完整。"""
    connection = Database(tmp_path / "fts-validation.db").open()
    _insert_claim(connection)
    backfill_index_text(
        connection,
        FakeEmbedder(8),
        mode="answerable",
        version="v1",
        batch_size=10,
        max_attempts=1,
    )
    rowid = connection.execute("SELECT rowid FROM claims WHERE id='claim-1'").fetchone()[0]
    connection.execute("DELETE FROM claims_fts_docsize WHERE id=?", (rowid,))
    connection.commit()

    result = backfill_index_text(
        connection,
        FakeEmbedder(8),
        mode="answerable",
        version="v1",
        batch_size=10,
        max_attempts=1,
    )

    assert result.failed == 0
    assert result.integrity_ok is False
    assert result.integrity is not None
    assert result.integrity["fts_missing_rows"] == 1
    assert not result.coverage_complete
    assert result.last_error_class == "IndexIntegrityError"


def test_backfill_cli_mode_overrides_settings(tmp_path, monkeypatch, capsys) -> None:
    """显式 --mode 优先于 Settings，并输出增强后的 dry-run 摘要。"""
    database_path = tmp_path / "cli-mode.db"
    database = Database(database_path)
    connection = database.open()
    _insert_claim(connection)
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    database.close()
    before = database_path.read_bytes()
    settings = replace(
        Settings(),
        database_path=str(database_path),
        embedding_dim=8,
        index_text_mode="legacy",
    )
    monkeypatch.setattr(cli_module, "load_settings", lambda *_: settings)

    cli_module.main(["backfill-index-text", "--mode", "answerable", "--dry-run"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "answerable"
    assert payload["would_update"] == 1
    assert payload["coverage_complete"] is True
    assert database_path.read_bytes() == before


def test_backfill_cli_dry_run_does_not_create_missing_database(tmp_path, monkeypatch) -> None:
    """dry-run 必须只读打开，不能通过 Database 自动创建或 migration。"""
    database_path = tmp_path / "missing.db"
    settings = replace(Settings(), database_path=str(database_path), embedding_dim=8)
    monkeypatch.setattr(cli_module, "load_settings", lambda *_: settings)

    with pytest.raises(sqlite3.OperationalError):
        cli_module.main(["backfill-index-text", "--mode", "answerable", "--dry-run"])

    assert not database_path.exists()


def test_backfill_cli_exits_nonzero_on_failure(tmp_path, monkeypatch, capsys) -> None:
    """failed 或覆盖不完整时 CLI 输出摘要后以非零状态退出。"""
    database_path = tmp_path / "cli-failed.db"
    connection = Database(database_path).open()
    _insert_claim(connection)
    connection.close()
    settings = replace(
        Settings(),
        database_path=str(database_path),
        embedding_dim=8,
    )

    class InvalidEmbedder(FakeEmbedder):
        def embed_batch(self, texts: list[str]) -> list[bytes]:
            raise ValueError("invalid embedding input")

    monkeypatch.setattr(cli_module, "load_settings", lambda *_: settings)
    monkeypatch.setattr(cli_module, "make_embedder", lambda _: InvalidEmbedder(8))

    with pytest.raises(SystemExit, match="1"):
        cli_module.main(["backfill-index-text", "--mode", "answerable"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["failed"] == 1
    assert payload["coverage_complete"] is False


def test_backfill_cli_exits_nonzero_on_integrity_failure(tmp_path, monkeypatch, capsys) -> None:
    """即使 provider 无失败，FTS 完整性失败也必须令 CLI 非零退出。"""
    database_path = tmp_path / "cli-integrity.db"
    database = Database(database_path)
    connection = database.open()
    _insert_claim(connection)
    backfill_index_text(
        connection,
        FakeEmbedder(8),
        mode="answerable",
        version="v1",
        batch_size=10,
        max_attempts=1,
    )
    rowid = connection.execute("SELECT rowid FROM claims WHERE id='claim-1'").fetchone()[0]
    connection.execute("DELETE FROM claims_fts_docsize WHERE id=?", (rowid,))
    connection.commit()
    database.close()
    settings = replace(Settings(), database_path=str(database_path), embedding_dim=8)
    monkeypatch.setattr(cli_module, "load_settings", lambda *_: settings)

    with pytest.raises(SystemExit, match="1"):
        cli_module.main(["backfill-index-text", "--mode", "answerable"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["failed"] == 0
    assert payload["integrity_ok"] is False
    assert payload["coverage_complete"] is False


def test_index_backfill_settings_validation() -> None:
    """新增回填参数必须为正数且版本非空。"""
    replace(Settings(), index_text_mode="answerable").validate()
    with pytest.raises(ConfigurationError):
        replace(Settings(), index_backfill_batch_size=0).validate()
    with pytest.raises(ConfigurationError):
        replace(Settings(), index_backfill_max_attempts=0).validate()
    with pytest.raises(ConfigurationError):
        replace(Settings(), index_text_version=" ").validate()
