"""记忆重分类任务测试。"""

from __future__ import annotations

from hl_mem.ingest.embeddings import pack_vector
from hl_mem.storage.database import Database
from hl_mem.storage.repository import ClaimRepository
from hl_mem.workers.reclassify import reclassify_claims

NOW = "2026-07-21T00:00:00+00:00"


def _claim(connection, claim_id="c"):
    assert ClaimRepository(connection).insert_claim(
        {
            "id": claim_id,
            "recorded_from": NOW,
            "status": "active",
            "subject_entity_id": "user",
            "predicate": "likes",
            "value_json": '"tea"',
            "confidence": 1.0,
            "importance": 0.5,
            "embedding_dense": pack_vector([1.0]),
        }
    )


def test_reclassify_batches_updates_and_is_idempotent(tmp_path, monkeypatch):
    connection = Database(tmp_path / "reclass.db").open()
    for index in range(6):
        _claim(connection, str(index))
    class FakeClient:
        """测试用 LLM 客户端；classify_batch 会被替换。"""

        model = "test"

    fake_client = FakeClient()
    calls = []

    def fake_batch(_client, claims):
        calls.append(len(claims))
        return [{"id": claim["id"], "scope": "temporal", "importance": 0.8} for claim in claims]

    monkeypatch.setattr("hl_mem.workers.reclassify.classify_batch", fake_batch)
    assert reclassify_claims(connection, fake_client, 5)["updated"] == 6
    assert calls == [5, 1]
    assert reclassify_claims(connection, fake_client, 5)["eligible"] == 0
