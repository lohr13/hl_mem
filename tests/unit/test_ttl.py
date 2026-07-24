from hl_mem.storage.database import Database
from hl_mem.storage.claims import ClaimRepository
from hl_mem.workers.ttl import expire_claims


def test_only_expired_ephemeral_active_claims_are_expired(tmp_path) -> None:
    connection = Database(tmp_path / "ttl.db").open()
    repo = ClaimRepository(connection)
    base = {"namespace_key": "default", "recorded_from": "2026-01-01", "status": "active"}
    repo.insert_claim({**base, "id": "past", "volatility": "ephemeral",
                       "expires_at": "2026-01-01T00:00:00+00:00"})
    repo.insert_claim({**base, "id": "future", "volatility": "ephemeral",
                       "expires_at": "2027-01-01T00:00:00+00:00"})
    repo.insert_claim({**base, "id": "stable", "volatility": "stable",
                       "expires_at": "2026-01-01T00:00:00+00:00"})
    assert expire_claims(connection, "2026-06-01T00:00:00+00:00") == {"expired": 1}
    statuses = {row["id"]: row["status"] for row in connection.execute(
        "SELECT id,status FROM claims").fetchall()}
    assert statuses == {"past": "expired", "future": "active", "stable": "active"}
