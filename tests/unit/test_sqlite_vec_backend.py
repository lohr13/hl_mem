"""sqlite-vec 派生索引、KNN 检索和正确性回退测试。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Never, cast

import pytest

pytest.importorskip("sqlite_vec")

from hl_mem.application.correction import CorrectionService
from hl_mem.application.forget import ForgetService
from hl_mem.core.vector import pack_vector
from hl_mem.domain.temporal import RecallIntent
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.settings import Settings, VectorBackend
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.storage.sqlite_vec import SQLiteVecVectorBackend
from hl_mem.workers.backfill_index_text import backfill_index_text
from hl_mem.workers.decay import decay_claims

REFERENCE_TIME = "2026-08-07T12:00:00+00:00"


class _BeforeClaimLookupConnection:
    """在 KNN 与 Claim 回表之间提交并发状态变化。"""

    def __init__(self, connection: Any, before_lookup: Callable[[], None]) -> None:
        self._connection = connection
        self._before_lookup = before_lookup
        self._injected = False

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> Any:
        if sql.startswith("SELECT * FROM claims WHERE id IN") and not self._injected:
            self._injected = True
            self._before_lookup()
        return self._connection.execute(sql, parameters)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def _settings() -> Settings:
    return replace(
        Settings.for_test(),
        vector_backend=VectorBackend.SQLITE_VEC,
        embedding_dim=3,
        embedding_model="test-embedding-3d",
        recall_vector_scan_limit=20,
    )


def _open_database(tmp_path: Path) -> tuple[Database, sqlite3.Connection, Settings]:
    settings = _settings()
    database = Database(tmp_path / "sqlite-vec.db", settings=settings)
    return database, database.open(), settings


def _claim(
    claim_id: str,
    vector: tuple[float, float, float],
    settings: Settings,
    *,
    namespace: str = "default",
    status: str = "active",
    recorded_from: str = "2026-08-01T00:00:00+00:00",
    expires_at: str | None = None,
    **overrides: object,
) -> dict[str, object]:
    claim: dict[str, object] = {
        "id": claim_id,
        "namespace_key": namespace,
        "subject_entity_id": claim_id,
        "predicate": "has-vector",
        "value": claim_id,
        "qualifiers": {},
        "recorded_from": recorded_from,
        "valid_from": "2026-08-01T00:00:00+00:00",
        "expires_at": expires_at,
        "status": status,
        "embedding_dense": pack_vector(vector),
        "embedding_model": settings.embedding_model,
        "embedding_dim": settings.embedding_dim,
        "topic_tags_json": "[]",
    }
    claim.update(overrides)
    return claim


def _insert_raw_claim(connection: sqlite3.Connection, claim: dict[str, object]) -> None:
    connection.execute(
        "INSERT INTO claims("
        "id,namespace_key,subject_entity_id,predicate,value_json,qualifiers_json,"
        "recorded_from,valid_from,expires_at,status,embedding_dense,embedding_model,embedding_dim,index_text"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            claim["id"],
            claim["namespace_key"],
            claim["subject_entity_id"],
            claim["predicate"],
            f'"{claim["value"]}"',
            "{}",
            claim["recorded_from"],
            claim["valid_from"],
            claim["expires_at"],
            claim["status"],
            claim["embedding_dense"],
            claim["embedding_model"],
            claim["embedding_dim"],
            claim["id"],
        ),
    )


def test_sqlite_vec_is_an_explicit_non_default_backend() -> None:
    assert Settings().vector_backend is VectorBackend.SQLITE_SCAN
    assert VectorBackend("sqlite_vec") is VectorBackend.SQLITE_VEC


def test_projection_crud_uses_delete_then_insert_and_clears_dirty(tmp_path: Path) -> None:
    database, connection, settings = _open_database(tmp_path)
    try:
        scan = ClaimRepository(connection, settings=replace(settings, vector_backend=VectorBackend.SQLITE_SCAN))
        backend = SQLiteVecVectorBackend(
            connection,
            embedding_dim=settings.embedding_dim,
            embedding_model=settings.embedding_model,
            scan_fallback=scan.search,
        )
        claim = _claim("crud", (1.0, 0.0, 0.0), settings)
        _insert_raw_claim(connection, claim)

        backend.insert("crud")

        assert connection.execute("SELECT claim_id FROM claims_vec_v1").fetchone()[0] == "crud"
        assert connection.execute("SELECT 1 FROM claim_vector_dirty WHERE claim_id='crud'").fetchone() is None

        replacement = pack_vector((0.0, 1.0, 0.0))
        connection.execute("UPDATE claims SET embedding_dense=? WHERE id='crud'", (replacement,))
        backend.update("crud")

        result = backend.search(
            replacement,
            1,
            REFERENCE_TIME,
            RecallIntent.CURRENT_STATE,
            None,
            "default",
        )
        assert [row["id"] for row in result] == ["crud"]
        assert result[0]["_score"] == pytest.approx(1.0, abs=1e-6)
        assert connection.execute("SELECT 1 FROM claim_vector_dirty WHERE claim_id='crud'").fetchone() is None

        backend.delete("crud")

        assert connection.execute("SELECT 1 FROM claims_vec_v1 WHERE claim_id='crud'").fetchone() is None
        assert connection.execute("SELECT 1 FROM claim_vector_dirty WHERE claim_id='crud'").fetchone() is None
    finally:
        database.close()


def test_knn_returns_exact_cosine_order_and_honors_known_as_of(tmp_path: Path) -> None:
    database, connection, settings = _open_database(tmp_path)
    try:
        repository = ClaimRepository(connection, settings=settings)
        repository.insert_claim(_claim("nearest", (1.0, 0.0, 0.0), settings))
        repository.insert_claim(_claim("second", (0.8, 0.6, 0.0), settings))
        repository.insert_claim(_claim("third", (0.0, 1.0, 0.0), settings))
        repository.insert_claim(
            _claim(
                "not-known-yet",
                (1.0, 0.0, 0.0),
                settings,
                recorded_from="2026-08-08T00:00:00+00:00",
            )
        )

        assert connection.execute("SELECT vec_version()").fetchone()[0].startswith("v0.1.")
        assert connection.execute("SELECT COUNT(*) FROM claims_vec_v1").fetchone()[0] == 4
        second_connection = database.open()
        assert second_connection.execute("SELECT vec_version()").fetchone()[0].startswith("v0.1.")

        results = repository.search_claims_vector(
            pack_vector((1.0, 0.0, 0.0)),
            3,
            REFERENCE_TIME,
            RecallIntent.CURRENT_STATE,
            REFERENCE_TIME,
            "default",
        )

        assert [row["id"] for row in results] == ["nearest", "second", "third"]
        assert [row["_score"] for row in results] == pytest.approx([1.0, 0.8, 0.0], abs=1e-6)
    finally:
        database.close()


def test_knn_partition_never_returns_another_namespace(tmp_path: Path) -> None:
    database, connection, settings = _open_database(tmp_path)
    try:
        repository = ClaimRepository(connection, settings=settings)
        repository.insert_claim(_claim("default-neighbor", (0.8, 0.6, 0.0), settings))
        repository.insert_claim(_claim("private-neighbor", (1.0, 0.0, 0.0), settings, namespace="private"))

        results = repository.search_claims_vector(
            pack_vector((1.0, 0.0, 0.0)),
            5,
            REFERENCE_TIME,
            RecallIntent.CURRENT_STATE,
            None,
            "default",
        )

        assert [row["id"] for row in results] == ["default-neighbor"]
        assert all(row["namespace_key"] == "default" for row in results)
    finally:
        database.close()


def test_oversample_cap_falls_back_to_sqlite_scan(tmp_path: Path) -> None:
    database, connection, settings = _open_database(tmp_path)
    try:
        repository = ClaimRepository(connection, settings=settings)
        for index, vector in enumerate(((1.0, 0.0, 0.0), (0.99, 0.01, 0.0), (0.98, 0.02, 0.0), (0.97, 0.03, 0.0))):
            repository.insert_claim(
                _claim(
                    f"expired-{index}",
                    vector,
                    settings,
                    expires_at="2026-08-06T00:00:00+00:00",
                )
            )
        repository.insert_claim(_claim("visible-a", (0.5, 0.5, 0.0), settings))
        repository.insert_claim(_claim("visible-b", (0.0, 1.0, 0.0), settings))
        scan = ClaimRepository(connection, settings=replace(settings, vector_backend=VectorBackend.SQLITE_SCAN))
        capped_backend = SQLiteVecVectorBackend(
            connection,
            embedding_dim=settings.embedding_dim,
            embedding_model=settings.embedding_model,
            scan_fallback=scan.search,
            max_probe=3,
        )

        results = capped_backend.search(
            pack_vector((1.0, 0.0, 0.0)),
            2,
            REFERENCE_TIME,
            RecallIntent.CURRENT_STATE,
            None,
            "default",
        )

        assert [row["id"] for row in results] == ["visible-a", "visible-b"]
    finally:
        database.close()


def test_oversampling_expands_from_three_to_six_to_twelve(tmp_path: Path) -> None:
    database, connection, settings = _open_database(tmp_path)
    try:
        repository = ClaimRepository(connection, settings=settings)
        for index in range(6):
            repository.insert_claim(
                _claim(
                    f"expired-neighbor-{index}",
                    (1.0, index * 0.001, 0.0),
                    settings,
                    expires_at="2026-08-06T00:00:00+00:00",
                )
            )
        repository.insert_claim(_claim("visible-after-six", (0.0, 1.0, 0.0), settings))

        def fail_if_scan_runs(*_args: object) -> Never:
            raise AssertionError("adaptive sqlite-vec probing should find the complete result")

        backend = SQLiteVecVectorBackend(
            connection,
            embedding_dim=settings.embedding_dim,
            embedding_model=settings.embedding_model,
            scan_fallback=fail_if_scan_runs,
        )

        results = backend.search(
            pack_vector((1.0, 0.0, 0.0)),
            1,
            REFERENCE_TIME,
            RecallIntent.CURRENT_STATE,
            None,
            "default",
        )

        assert [row["id"] for row in results] == ["visible-after-six"]
    finally:
        database.close()


def test_knn_and_claim_lookup_share_one_read_snapshot(tmp_path: Path) -> None:
    database, connection, settings = _open_database(tmp_path)
    concurrent = database.open()
    try:
        repository = ClaimRepository(connection, settings=settings)
        repository.insert_claim(_claim("concurrent", (1.0, 0.0, 0.0), settings))

        def retract_concurrently() -> None:
            concurrent.execute("UPDATE claims SET status='retracted' WHERE id='concurrent'")
            concurrent.commit()

        wrapped = _BeforeClaimLookupConnection(connection, retract_concurrently)
        scan = ClaimRepository(connection, settings=replace(settings, vector_backend=VectorBackend.SQLITE_SCAN))
        backend = SQLiteVecVectorBackend(
            cast(sqlite3.Connection, wrapped),
            embedding_dim=settings.embedding_dim,
            embedding_model=settings.embedding_model,
            scan_fallback=scan.search,
        )

        results = backend.search(
            pack_vector((1.0, 0.0, 0.0)),
            1,
            REFERENCE_TIME,
            RecallIntent.CURRENT_STATE,
            None,
            "default",
        )

        assert [row["id"] for row in results] == ["concurrent"]
        assert connection.execute("SELECT status FROM claims WHERE id='concurrent'").fetchone()[0] == "retracted"
    finally:
        database.close()


def test_dirty_probe_falls_back_before_reading_stale_vec_rows(tmp_path: Path) -> None:
    database, connection, settings = _open_database(tmp_path)
    try:
        repository = ClaimRepository(connection, settings=settings)
        repository.insert_claim(_claim("changed", (1.0, 0.0, 0.0), settings))
        repository.insert_claim(_claim("unchanged", (0.0, 1.0, 0.0), settings))
        changed_vector = pack_vector((0.0, 0.0, 1.0))
        connection.execute("UPDATE claims SET embedding_dense=? WHERE id='changed'", (changed_vector,))
        scan = ClaimRepository(connection, settings=replace(settings, vector_backend=VectorBackend.SQLITE_SCAN))
        backend = SQLiteVecVectorBackend(
            connection,
            embedding_dim=settings.embedding_dim,
            embedding_model=settings.embedding_model,
            scan_fallback=scan.search,
        )

        results = backend.search(
            changed_vector,
            1,
            REFERENCE_TIME,
            RecallIntent.CURRENT_STATE,
            None,
            "default",
        )

        assert (
            connection.execute("SELECT reason FROM claim_vector_dirty WHERE claim_id='changed'").fetchone() is not None
        )
        assert [row["id"] for row in results] == ["changed"]
        assert results[0]["_score"] == pytest.approx(1.0, abs=1e-6)
    finally:
        database.close()


def test_valid_resync_repairs_the_last_degraded_projection(tmp_path: Path) -> None:
    database, connection, settings = _open_database(tmp_path)
    try:
        repository = ClaimRepository(connection, settings=settings)
        repository.insert_claim(_claim("repairable", (0.0, 0.0, 0.0), settings))

        assert (
            connection.execute("SELECT build_status FROM vector_index_state WHERE backend='sqlite_vec'").fetchone()[0]
            == "degraded"
        )

        connection.execute(
            "UPDATE claims SET embedding_dense=? WHERE id='repairable'",
            (pack_vector((1.0, 0.0, 0.0)),),
        )
        repository.sync_vector("repairable")

        state = connection.execute(
            "SELECT build_status,last_error FROM vector_index_state WHERE backend='sqlite_vec'"
        ).fetchone()
        assert tuple(state) == ("ready", None)
        assert connection.execute("SELECT 1 FROM claim_vector_dirty LIMIT 1").fetchone() is None
        assert [
            row["id"]
            for row in repository.search_claims_vector(
                pack_vector((1.0, 0.0, 0.0)),
                1,
                REFERENCE_TIME,
                RecallIntent.CURRENT_STATE,
                None,
                "default",
            )
        ] == ["repairable"]
    finally:
        database.close()


def test_forget_clears_projection_and_dirty_marker_in_same_transaction(tmp_path: Path) -> None:
    database, connection, settings = _open_database(tmp_path)
    try:
        repository = ClaimRepository(connection, settings=settings)
        repository.insert_claim(_claim("forgotten", (1.0, 0.0, 0.0), settings))

        ForgetService(connection).forget("forgotten")

        assert connection.execute("SELECT 1 FROM claims_vec_v1 WHERE claim_id='forgotten'").fetchone() is None
        assert connection.execute("SELECT 1 FROM claim_vector_dirty WHERE claim_id='forgotten'").fetchone() is None
    finally:
        database.close()


def test_correction_inherits_connection_vector_backend_when_settings_are_omitted(tmp_path: Path) -> None:
    database, connection, settings = _open_database(tmp_path)
    try:
        repository = ClaimRepository(connection, settings=settings)
        repository.insert_claim(_claim("corrected", (1.0, 0.0, 0.0), settings))

        CorrectionService(connection, FakeEmbedder(3)).apply(
            "corrected",
            action="retract",
            corrected_text=None,
            idempotency_key="sqlite-vec-correction",
        )

        assert connection.execute("SELECT 1 FROM claims_vec_v1 WHERE claim_id='corrected'").fetchone() is None
        assert connection.execute("SELECT 1 FROM claim_vector_dirty WHERE claim_id='corrected'").fetchone() is None
    finally:
        database.close()


def test_decay_archive_removes_projection_without_leaving_dirty(tmp_path: Path) -> None:
    database, connection, settings = _open_database(tmp_path)
    try:
        recorded_from = (datetime.fromisoformat(REFERENCE_TIME) - timedelta(days=400)).isoformat()
        repository = ClaimRepository(connection, settings=settings)
        repository.insert_claim(
            _claim(
                "archived",
                (1.0, 0.0, 0.0),
                settings,
                recorded_from=recorded_from,
                scope="temporal",
                last_accessed_at=recorded_from,
            )
        )

        decay_claims(
            connection,
            REFERENCE_TIME,
            temporal_decay_days=90,
            temporal_archive_days=180,
            permanent_decay_days=180,
            permanent_archive_days=365,
            access_bonus_every=10,
            access_bonus_days=30,
            access_bonus_cap_days=365,
            rollout_grace_days=7,
            min_confidence=0.05,
            feedback_lifecycle_mode="observe",
            feedback_bonus_cap_days=180,
        )

        assert connection.execute("SELECT 1 FROM claims_vec_v1 WHERE claim_id='archived'").fetchone() is None
        assert connection.execute("SELECT 1 FROM claim_vector_dirty WHERE claim_id='archived'").fetchone() is None
    finally:
        database.close()


def test_embedding_backfill_updates_projection_without_leaving_dirty(tmp_path: Path) -> None:
    settings = replace(_settings(), embedding_model="fake")
    database = Database(tmp_path / "backfill.db", settings=settings)
    connection = database.open()
    try:
        repository = ClaimRepository(connection, settings=settings)
        repository.insert_claim(
            _claim(
                "backfilled",
                (1.0, 0.0, 0.0),
                settings,
                index_text="stale index text",
            )
        )

        summary = backfill_index_text(
            connection,
            FakeEmbedder(3),
            mode="legacy",
            version="sqlite-vec-test",
            batch_size=10,
            max_attempts=1,
        )

        assert summary.backfilled == 1
        assert connection.execute("SELECT 1 FROM claim_vector_dirty WHERE claim_id='backfilled'").fetchone() is None
        assert connection.execute("SELECT 1 FROM claims_vec_v1 WHERE claim_id='backfilled'").fetchone() is not None
    finally:
        database.close()
