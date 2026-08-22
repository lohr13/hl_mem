"""M4 内联替代、召回意图与双时间语义测试。"""

import json

import pytest

from hl_mem.application.recall import RecallService
from hl_mem.core.vector import pack_vector
from hl_mem.domain.recall import route_recall_intent
from hl_mem.domain.temporal import RecallIntent, canonical_utc_iso, claim_is_visible, parse_utc
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.workers.ttl import expire_claims


def _claim(connection, claim_id: str, **values):
    row = {
        "id": claim_id,
        "namespace_key": "default",
        "recorded_from": "2026-01-02T00:00:00Z",
        "valid_from": "2026-01-01T00:00:00Z",
        "status": "active",
        "scope": "permanent",
        "value_json": json.dumps(values.pop("value", claim_id)),
        "embedding_dense": pack_vector([1.0]),
    }
    row.update(values)
    assert ClaimRepository(connection).insert_claim(row)


def test_supersede_with_inline_preserves_bitemporal_values_and_is_idempotent(
    tmp_path,
) -> None:
    connection = Database(tmp_path / "supersede.db").open()
    _claim(connection, "old", value="深色模式")
    _claim(connection, "new", value="浅色模式", valid_from="2026-02-01T00:00:00Z")
    repo = ClaimRepository(connection)

    first = repo.supersede_with_inline("old", "new", "浅色模式", "2026-02-01T00:00:00Z", "2026-02-02T00:00:00Z")
    second = repo.supersede_with_inline("old", "new", "浅色模式", "2026-02-01T00:00:00Z", "2026-02-02T00:00:00Z")

    old = repo.get_claim("old")
    assert first.applied is True and second.applied is False
    assert old["status"] == "superseded"
    assert (old["valid_to"], old["recorded_to"], old["superseded_by_id"]) == (
        "2026-02-01T00:00:00Z",
        "2026-02-02T00:00:00Z",
        "new",
    )
    assert old["value"]["old_value"] == "深色模式"
    assert (
        connection.execute(
            "SELECT count(*) FROM evidence_links WHERE derived_id='new' AND evidence_id='old' "
            "AND relation='supersedes'"
        ).fetchone()[0]
        == 1
    )


@pytest.mark.parametrize(
    ("query", "as_of", "expected"),
    [
        ("现在用什么", None, RecallIntent.CURRENT_STATE),
        ("以前用什么", None, RecallIntent.HISTORICAL),
        ("普通查询", "2025-01-01T00:00:00Z", RecallIntent.CURRENT_STATE),
    ],
)
def test_route_recall_intent(query, as_of, expected) -> None:
    assert route_recall_intent(query, as_of, now="2026-01-01T00:00:00Z") is expected


def test_visibility_uses_half_open_valid_and_recorded_intervals() -> None:
    claim = {
        "status": "superseded",
        "scope": "permanent",
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_to": "2026-02-01T00:00:00Z",
        "recorded_from": "2026-01-10T00:00:00Z",
        "recorded_to": "2026-03-01T00:00:00Z",
    }
    assert claim_is_visible(claim, "2026-01-15T00:00:00Z", "2026-02-01T00:00:00Z", RecallIntent.HISTORICAL)
    assert claim_is_visible(claim, "2026-01-15T00:00:00Z", None, RecallIntent.CURRENT_STATE)
    assert claim_is_visible(claim, "2026-02-01T00:00:00Z", None, RecallIntent.HISTORICAL)
    assert not claim_is_visible(claim, "2026-02-01T00:00:00Z", None, RecallIntent.CURRENT_STATE)
    assert not claim_is_visible(claim, "2026-01-05T00:00:00Z", "2026-01-05T00:00:00Z", RecallIntent.HISTORICAL)
    assert parse_utc("2026-01-01T08:00:00+08:00") == parse_utc("2026-01-01T00:00:00Z")
    with pytest.raises(ValueError, match="invalid ISO-8601"):
        parse_utc("bad")


def test_canonical_utc_identity_normalizes_offsets_and_rejects_naive_time() -> None:
    assert canonical_utc_iso("2026-08-22T16:00:00+08:00") == "2026-08-22T08:00:00+00:00"
    assert canonical_utc_iso("2026-08-22T08:00:00Z") == "2026-08-22T08:00:00+00:00"
    with pytest.raises(ValueError, match="without timezone"):
        canonical_utc_iso("2026-08-22T08:00:00")


def test_historical_state_context_is_visible_only_to_historical_intent() -> None:
    claim = {
        "status": "active",
        "valid_from": "2026-08-20T00:00:00Z",
        "valid_to": None,
        "recorded_from": "2026-08-22T00:00:00Z",
        "qualifiers": {"_state_context": "historical"},
    }

    assert not claim_is_visible(claim, "2026-08-22T00:00:00Z", None, RecallIntent.CURRENT_STATE)
    assert claim_is_visible(claim, "2026-08-22T00:00:00Z", None, RecallIntent.HISTORICAL)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("active", True),
        ("archived", True),
        ("superseded", True),
        ("expired", True),
        ("disputed", False),
        ("retracted", False),
    ],
)
def test_historical_visibility_uses_auditable_statuses(status, expected) -> None:
    claim = {
        "status": status,
        "valid_from": "2026-01-01T00:00:00Z",
        "recorded_from": "2026-01-01T00:00:00Z",
    }

    assert claim_is_visible(claim, "2026-02-01T00:00:00Z", None, RecallIntent.HISTORICAL) is expected


def test_historical_fts_candidates_include_archived_claims(tmp_path) -> None:
    connection = Database(tmp_path / "archived-history.db").open()
    _claim(
        connection,
        "archived",
        value="legacy archive marker",
        status="archived",
        embedding_dense=None,
    )
    repository = ClaimRepository(connection)

    historical = repository.search_claims_fts(
        "archive marker",
        as_of="2026-02-01T00:00:00Z",
        intent=RecallIntent.HISTORICAL,
    )
    current = repository.search_claims_fts(
        "archive marker",
        as_of="2026-02-01T00:00:00Z",
        intent=RecallIntent.CURRENT_STATE,
    )

    assert [claim["id"] for claim in historical] == ["archived"]
    assert current == []


def test_historical_fts_returns_closed_versions_before_as_of(tmp_path) -> None:
    connection = Database(tmp_path / "closed-history.db").open()
    _claim(
        connection,
        "old",
        value="legacy archive marker",
        status="superseded",
        valid_to="2026-01-15T00:00:00Z",
    )
    repository = ClaimRepository(connection)

    historical = repository.search_claims_fts(
        "archive marker",
        as_of="2026-02-01T00:00:00Z",
        intent=RecallIntent.HISTORICAL,
    )
    current = repository.search_claims_fts(
        "archive marker",
        as_of="2026-02-01T00:00:00Z",
        intent=RecallIntent.CURRENT_STATE,
    )

    assert [claim["id"] for claim in historical] == ["old"]
    assert current == []


def test_recall_passes_ranking_now_independently_from_as_of(tmp_path, monkeypatch) -> None:
    connection = Database(tmp_path / "ranking-clock.db").open()
    captured: dict[str, object] = {}

    def capture_hybrid(*args, **kwargs):
        captured["as_of"] = args[4]
        captured["intent"] = kwargs["intent"]
        captured["now"] = kwargs["now"]
        return []

    monkeypatch.setattr("hl_mem.application.recall.hybrid_claims", capture_hybrid)
    RecallService(connection, FakeEmbedder(4)).recall(
        "普通查询",
        as_of="2025-01-01T00:00:00Z",
        intent=RecallIntent.CURRENT_STATE,
        ranking_now="2025-01-02T00:00:00Z",
    )

    assert captured == {
        "as_of": "2025-01-01T00:00:00Z",
        "intent": RecallIntent.CURRENT_STATE,
        "now": "2025-01-02T00:00:00Z",
    }


def test_ttl_closes_valid_interval_but_remains_historically_visible(tmp_path) -> None:
    connection = Database(tmp_path / "ttl-history.db").open()
    _claim(
        connection,
        "old",
        volatility="ephemeral",
        scope="temporal",
        expires_at="2026-01-20T00:00:00Z",
    )
    assert expire_claims(
        connection,
        "2026-01-21T00:00:00Z",
        feedback_lifecycle_mode="observe",
        slot_short_ttl_seconds=86400,
    ) == {"expired": 1}
    claim = ClaimRepository(connection).get_claim("old")
    assert claim["valid_to"] == "2026-01-20T00:00:00+00:00"
    assert claim_is_visible(claim, "2026-01-19T00:00:00Z", None, RecallIntent.HISTORICAL)
