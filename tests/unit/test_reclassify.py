"""记忆重分类任务测试。"""

from __future__ import annotations

from hl_mem.application.version_report import report_version
from hl_mem.domain.claims.conflicts import compute_conflict_key
from hl_mem.ingest.embedder import pack_vector
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
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


def test_reclassify_guard_skips_active_claim_that_would_collide_with_exclusive_group(tmp_path, monkeypatch) -> None:
    connection = Database(tmp_path / "reclass-guard.db").open()
    target_key = compute_conflict_key("default", "user", "配置", "config.port", {"service": "api"})
    assert target_key is not None
    repository = ClaimRepository(connection)
    common = {
        "namespace_key": "default",
        "recorded_from": NOW,
        "status": "active",
        "subject_entity_id": "user",
        "predicate": "配置",
        "qualifiers": {"service": "api"},
        "canonical_attribute": "config.port",
        "canonical_slot": "config.port",
        "confidence": 1.0,
        "importance": 0.5,
        "scope": "permanent",
        "embedding_dense": pack_vector([1.0]),
    }
    assert repository.insert_claim({**common, "id": "a-moving", "value": "8080", "conflict_key": "stale-key"})
    assert repository.insert_claim({**common, "id": "b-occupied", "value": "8081", "conflict_key": target_key})

    class FakeClient:
        model = "test"

    monkeypatch.setattr(
        "hl_mem.workers.reclassify.classify_batch",
        lambda _client, claims: [{"id": claim["id"], "scope": "temporal", "importance": 0.8} for claim in claims],
    )

    result = reclassify_claims(connection, FakeClient(), 5)

    assert result == {"scanned": 2, "eligible": 2, "updated": 1, "guarded": 1}
    moving = repository.get_claim("a-moving")
    assert moving["conflict_key"] == "stale-key"
    assert moving["scope"] == "permanent"
    assert moving["importance"] == 0.5


def test_reclassify_skips_deterministic_version_probe(tmp_path, monkeypatch) -> None:
    connection = Database(tmp_path / "version-probe.db").open()
    report = report_version(connection, namespace="default", subject="HL-Mem")
    claim_id = connection.execute(
        "SELECT derived_id FROM evidence_links WHERE evidence_type='event' AND evidence_id=?",
        (report["event_id"],),
    ).fetchone()[0]

    monkeypatch.setattr(
        "hl_mem.workers.reclassify.classify_batch",
        lambda _client, claims: [{"id": claim["id"], "scope": "permanent", "importance": 0.7} for claim in claims],
    )

    result = reclassify_claims(connection, object(), 5)

    claim = ClaimRepository(connection).get_claim(claim_id)
    assert result["eligible"] == 0
    assert claim is not None
    assert claim["canonical_slot"] == "config.version"
    assert claim["importance"] == 0.5


def test_reclassify_does_not_protect_unproven_version_observation(tmp_path, monkeypatch) -> None:
    connection = Database(tmp_path / "unproven-version-observation.db").open()
    repository = ClaimRepository(connection)
    assert repository.insert_claim(
        {
            "id": "unproven-version-observation",
            "namespace_key": "default",
            "recorded_from": NOW,
            "observed_at": NOW,
            "status": "active",
            "subject_entity_id": "hl_mem",
            "predicate": "配置",
            "value": "0.32.0",
            "canonical_attribute": "config.version",
            "canonical_slot": "config.version",
            "assertion_kind": "observation",
            "source_authority": "high",
            "scope": "permanent",
            "importance": 0.5,
        }
    )
    monkeypatch.setattr(
        "hl_mem.workers.reclassify.classify_batch",
        lambda _client, claims: [{"id": claim["id"], "scope": "permanent", "importance": 0.7} for claim in claims],
    )

    result = reclassify_claims(connection, object(), 5)

    claim = repository.get_claim("unproven-version-observation")
    assert result["eligible"] == 1
    assert claim is not None
    assert claim["canonical_slot"] is None
    assert claim["importance"] == 0.7


def test_reclassify_treats_non_object_probe_event_content_as_unproven(tmp_path, monkeypatch) -> None:
    connection = Database(tmp_path / "non-object-probe-event.db").open()
    report = report_version(connection, namespace="default", subject="HL-Mem")
    claim_id = connection.execute(
        "SELECT derived_id FROM evidence_links WHERE evidence_type='event' AND evidence_id=?",
        (report["event_id"],),
    ).fetchone()[0]
    connection.execute("UPDATE events SET content_json='[]' WHERE id=?", (report["event_id"],))
    monkeypatch.setattr(
        "hl_mem.workers.reclassify.classify_batch",
        lambda _client, claims: [{"id": claim["id"], "scope": "permanent", "importance": 0.7} for claim in claims],
    )

    result = reclassify_claims(connection, object(), 5)

    claim = ClaimRepository(connection).get_claim(claim_id)
    assert result["eligible"] == 1
    assert claim is not None
    assert claim["canonical_slot"] is None
