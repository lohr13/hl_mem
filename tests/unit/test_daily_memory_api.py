from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import NoReturn

from fastapi.testclient import TestClient

from hl_mem.api.server import create_app
from hl_mem.application.recall import RecallService
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.workers.worker import Worker


def _insert_claim(
    repository: ClaimRepository,
    claim_id: str,
    text: str,
    recorded_from: str,
    *,
    namespace: str = "default",
    status: str = "active",
) -> None:
    assert repository.insert_claim(
        {
            "id": claim_id,
            "namespace_key": namespace,
            "subject_entity_id": "user",
            "predicate": "explicit_memory",
            "value_json": f'"{text}"',
            "index_text": text,
            "recorded_from": recorded_from,
            "status": status,
            "confidence": 1.0,
            "importance": 1.0,
        }
    )


def test_list_memories_api_pages_active_namespace_results(tmp_path: Path) -> None:
    database_path = tmp_path / "memories.db"
    database = Database(database_path)
    connection = database.open()
    repository = ClaimRepository(connection)
    _insert_claim(repository, "old", "旧记忆", "2026-08-01T00:00:00+00:00")
    _insert_claim(repository, "new", "新记忆", "2026-08-03T00:00:00+00:00")
    _insert_claim(repository, "other", "其他空间", "2026-08-04T00:00:00+00:00", namespace="other")
    _insert_claim(repository, "gone", "已撤回", "2026-08-05T00:00:00+00:00", status="retracted")
    database.close()

    with TestClient(create_app(replace(Settings.for_test(), database_path=str(database_path)))) as client:
        response = client.get(
            "/v1/memories",
            params={"namespace": "default", "status": "active", "limit": 1, "offset": 1},
        )

    assert response.status_code == 200
    assert response.json() == {
        "memories": [
            {
                "id": "old",
                "text": "旧记忆",
                "status": "active",
                "recorded_from": "2026-08-01T00:00:00+00:00",
                "valid_from": None,
                "canonical_slot": None,
                "topic_tags": [],
            }
        ],
        "total": 2,
        "limit": 1,
        "offset": 1,
    }


class _NeverEmbed:
    def embed_one(self, text: str) -> NoReturn:
        raise AssertionError(f"dense-off recall must not embed query: {text}")


def test_dense_off_recall_uses_fts_without_embedding_query(tmp_path: Path) -> None:
    database = Database(tmp_path / "fts-only.db")
    connection = database.open()
    _insert_claim(
        ClaimRepository(connection),
        "keyword-match",
        "SQLite WAL 模式",
        "2026-08-03T00:00:00+00:00",
    )
    settings = replace(Settings.for_test(), recall_dense_enabled=False)

    response = RecallService(connection, _NeverEmbed(), settings=settings).recall("SQLite WAL", debug=True)

    assert [item["id"] for item in response["results"]] == ["keyword-match"]
    assert response["search_trace"]["candidates"]["keyword-match"]["channels"] == {"fts": 1}
    database.close()


def test_no_key_worker_does_not_schedule_llm_maintenance_jobs(tmp_path: Path) -> None:
    settings = replace(
        Settings.for_test(),
        database_path=str(tmp_path / "offline-worker.db"),
        consolidate_cron="00:00",
        dedup_cron="00:00",
        dedup_enabled=True,
    )
    worker = Worker(settings)

    worker._run_maintenance()

    job_types = {
        row[0]
        for row in worker.connection.execute(
            "SELECT job_type FROM jobs WHERE job_type IN ('consolidate_conflicts','deduplicate_claims')"
        )
    }
    assert job_types == set()
    worker.database.close()
