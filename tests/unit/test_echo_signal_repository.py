from __future__ import annotations

from hl_mem.storage.database import Database
from hl_mem.storage.evidence import EvidenceRepository

NOW = "2026-08-18T12:00:00+00:00"


def test_echo_signal_repository_batches_session_resolution_and_ingest_pending_endpoint(tmp_path) -> None:
    """Catches N/A session provenance or a legacy/right-endpoint guess being exposed as a policy signal."""
    connection = Database(tmp_path / "echo-signals.db").open()
    connection.executemany(
        "INSERT INTO claims(id,namespace_key,value_json,recorded_from,status) VALUES (?,'default','\"x\"',?,'active')",
        ((claim_id, NOW) for claim_id in ("recent", "missing", "cross", "left")),
    )
    connection.executemany(
        "INSERT INTO events(id,tenant_id,session_id,event_type,actor_type,content_json,occurred_at,recorded_at) "
        "VALUES (?,'default',?,'message','user','{}',?,?)",
        (
            ("event-recent", "session-1", NOW, "2026-08-18T11:45:00+00:00"),
            ("event-missing", None, NOW, "2026-08-18T11:50:00+00:00"),
            ("event-cross", "session-2", NOW, "2026-08-18T11:55:00+00:00"),
        ),
    )
    connection.executemany(
        "INSERT INTO evidence_links(id,derived_type,derived_id,evidence_type,evidence_id,relation) "
        "VALUES (?,'claim',?,'event',?,'derived_from')",
        (
            ("link-recent", "recent", "event-recent"),
            ("link-missing", "missing", "event-missing"),
            ("link-cross", "cross", "event-cross"),
        ),
    )
    connection.execute(
        "INSERT INTO dedup_pairs("
        "id,pair_key,left_claim_id,right_claim_id,new_claim_id,pair_source,similarity,created_at"
        ") VALUES ('pending','left:recent','left','recent','recent','ingest',0.96,?)",
        ("2026-08-18T11:00:00+00:00",),
    )
    connection.commit()

    signals = EvidenceRepository(connection).batch_get_echo_signals(
        ["recent", "missing", "cross"],
        namespace="default",
        session_id="session-1",
    )

    assert signals == {
        "recent": {
            "source_session_resolved": True,
            "matching_session_recorded_at": "2026-08-18T11:45:00+00:00",
            "pending_similarity": 0.96,
            "pending_created_at": "2026-08-18T11:00:00+00:00",
        },
        "missing": {
            "source_session_resolved": False,
            "matching_session_recorded_at": None,
            "pending_similarity": None,
            "pending_created_at": None,
        },
        "cross": {
            "source_session_resolved": True,
            "matching_session_recorded_at": None,
            "pending_similarity": None,
            "pending_created_at": None,
        },
    }
