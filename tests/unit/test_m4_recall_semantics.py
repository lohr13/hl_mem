"""M4 内联替代、召回意图与双时间语义测试。"""

import json

import pytest

from hl_mem.ingest.embeddings import pack_vector
from hl_mem.recall.policy import RecallIntent, claim_is_visible, parse_utc, route_recall_intent
from hl_mem.storage.database import Database
from hl_mem.storage.repository import ClaimRepository
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


def test_supersede_with_inline_preserves_bitemporal_values_and_is_idempotent(tmp_path) -> None:
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
    assert json.loads(old["value_json"])["old_value"] == "深色模式"
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
        ("普通查询", "2025-01-01T00:00:00Z", RecallIntent.HISTORICAL),
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
    assert not claim_is_visible(claim, "2026-02-01T00:00:00Z", None, RecallIntent.HISTORICAL)
    assert not claim_is_visible(claim, "2026-01-05T00:00:00Z", "2026-01-05T00:00:00Z", RecallIntent.HISTORICAL)
    assert parse_utc("2026-01-01T08:00:00+08:00") == parse_utc("2026-01-01T00:00:00Z")
    with pytest.raises(ValueError, match="invalid ISO-8601"):
        parse_utc("bad")


def test_ttl_closes_valid_interval_but_remains_historically_visible(tmp_path) -> None:
    connection = Database(tmp_path / "ttl-history.db").open()
    _claim(connection, "old", volatility="ephemeral", scope="temporal", expires_at="2026-01-20T00:00:00Z")
    assert expire_claims(connection, "2026-01-21T00:00:00Z") == {"expired": 1}
    claim = ClaimRepository(connection).get_claim("old")
    assert claim["valid_to"] == "2026-01-20T00:00:00Z"
    assert claim_is_visible(claim, "2026-01-19T00:00:00Z", None, RecallIntent.HISTORICAL)
