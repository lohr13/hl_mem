from __future__ import annotations

from pathlib import Path

from hl_mem.storage.database import Database
from hl_mem.workers.repair_active_claims import audit_active_claims

NOW = "2026-08-15T08:00:00+00:00"


def _insert_claim(connection, claim_id: str) -> None:
    connection.execute(
        "INSERT INTO claims(id,namespace_key,predicate,value_json,recorded_from,status) "
        "VALUES (?, 'default', 'knows', ?, ?, 'active')",
        (claim_id, f'"{claim_id}"', NOW),
    )


def test_audit_classifies_dangling_references_without_exposing_content(tmp_path: Path) -> None:
    database = Database(tmp_path / "dangling.db")
    connection = database.open()
    _insert_claim(connection, "anchor")
    _insert_claim(connection, "supersedes-ref")
    _insert_claim(connection, "superseded-by-ref")
    connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute(
        "INSERT INTO evidence_links("
        "id,derived_type,derived_id,evidence_type,evidence_id,relation"
        ") VALUES ('missing-event-link','claim','anchor','event','missing-event','supports')"
    )
    connection.execute(
        "INSERT INTO evidence_links("
        "id,derived_type,derived_id,evidence_type,evidence_id,relation"
        ") VALUES ('missing-derivation-link','observation','missing-observation','claim','anchor','supports')"
    )
    connection.execute(
        "INSERT INTO memory_relations(id,from_id,to_id,relation,created_at) "
        "VALUES ('missing-endpoint','anchor','missing-claim','supports',?)",
        (NOW,),
    )
    connection.execute("UPDATE claims SET supersedes_id='missing-superseded' WHERE id='supersedes-ref'")
    connection.execute("UPDATE claims SET superseded_by_id='missing-successor' WHERE id='superseded-by-ref'")
    connection.commit()
    before = connection.total_changes

    report = audit_active_claims(connection)

    dangling = report["dangling_references"]
    assert report["healthy"] is False
    assert dangling["total_count"] == 5
    assert dangling["evidence_links"] == {
        "count": 1,
        "samples": [
            {
                "id": "missing-event-link",
                "derived_type": "claim",
                "derived_id": "anchor",
                "evidence_type": "event",
                "evidence_id": "missing-event",
                "missing": ["evidence"],
            }
        ],
    }
    assert dangling["relation_endpoints"] == {
        "count": 1,
        "samples": [
            {
                "id": "missing-endpoint",
                "from_id": "anchor",
                "to_id": "missing-claim",
                "missing": ["to"],
            }
        ],
    }
    assert dangling["derivation_supersede_references"] == {
        "count": 3,
        "samples": [
            {
                "kind": "derivation",
                "id": "missing-derivation-link",
                "source_id": "missing-observation",
                "target_id": "anchor",
                "missing": ["derivation"],
            },
            {
                "kind": "superseded_by_id",
                "id": "superseded-by-ref",
                "source_id": "superseded-by-ref",
                "target_id": "missing-successor",
                "missing": ["target"],
            },
            {
                "kind": "supersedes_id",
                "id": "supersedes-ref",
                "source_id": "supersedes-ref",
                "target_id": "missing-superseded",
                "missing": ["target"],
            },
        ],
    }
    assert connection.total_changes == before
    database.close()


def test_audit_caps_each_dangling_category_at_five_samples(tmp_path: Path) -> None:
    database = Database(tmp_path / "sample-limit.db")
    connection = database.open()
    _insert_claim(connection, "anchor")
    connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")
    for index in range(7):
        connection.execute(
            "INSERT INTO evidence_links("
            "id,derived_type,derived_id,evidence_type,evidence_id,relation"
            ") VALUES (?, 'claim', 'anchor', 'event', ?, 'supports')",
            (f"link-{index}", f"missing-event-{index}"),
        )
        connection.execute(
            "INSERT INTO memory_relations(id,from_id,to_id,relation,created_at) "
            "VALUES (?, 'anchor', ?, 'supports', ?)",
            (f"relation-{index}", f"missing-claim-{index}", NOW),
        )
        connection.execute(
            "INSERT INTO evidence_links("
            "id,derived_type,derived_id,evidence_type,evidence_id,relation"
            ") VALUES (?, 'observation', ?, 'claim', 'anchor', 'supports')",
            (f"observation-link-{index}", f"missing-observation-{index}"),
        )
    connection.commit()

    dangling = audit_active_claims(connection)["dangling_references"]

    assert dangling["total_count"] == 21
    for category in (
        "evidence_links",
        "relation_endpoints",
        "derivation_supersede_references",
    ):
        assert dangling[category]["count"] == 7
        assert len(dangling[category]["samples"]) == 5
    database.close()
