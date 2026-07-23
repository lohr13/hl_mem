"""一跳关系扩展召回的单元测试。"""

from __future__ import annotations

from hl_mem.domain.relations import get_relations_batch
from hl_mem.recall.policy import RecallIntent
from hl_mem.recall.relation_expansion import RelationExpansionConfig, expand_related_claims
from hl_mem.storage.database import Database
from hl_mem.storage.repository import ClaimRepository

NOW = "2026-07-24T00:00:00+00:00"


def _insert_claim(
    connection,
    claim_id: str,
    *,
    namespace: str = "default",
    status: str = "active",
    valid_from: str = "2026-01-01T00:00:00+00:00",
    valid_to: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO claims "
        "(id,namespace_key,subject_entity_id,predicate,value_json,status,confidence,importance,"
        "valid_from,valid_to,recorded_from,recorded_to) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            claim_id,
            namespace,
            "user",
            "preference",
            f'"{claim_id}"',
            status,
            1.0,
            0.5,
            valid_from,
            valid_to,
            valid_from,
            None,
        ),
    )


def test_get_relations_batch_expands_memory_and_reverse_claim_evidence(tmp_path) -> None:
    database = Database(tmp_path / "relations.db")
    try:
        with database.connect() as connection:
            for claim_id in ("seed", "memory-out", "memory-in", "evidence-out", "evidence-in"):
                _insert_claim(connection, claim_id)
            connection.execute(
                "INSERT INTO memory_relations(id,from_id,to_id,relation,confidence,evidence_json,created_at) "
                "VALUES ('m1','seed','memory-out','supports',0.8,'[]',?),"
                "('m2','memory-in','seed','about',0.7,'[]',?)",
                (NOW, NOW),
            )
            connection.execute(
                "INSERT INTO evidence_links(id,derived_type,derived_id,evidence_type,evidence_id,relation,weight) "
                "VALUES ('e1','claim','seed','claim','evidence-out','derived_from',0.9),"
                "('e2','claim','evidence-in','claim','seed','follows',0.6)"
            )
            connection.commit()

            default_result = get_relations_batch(connection, ["seed"])
            expanded = get_relations_batch(
                connection,
                ["seed"],
                include_memory_relations=True,
                include_reverse_evidence=True,
            )
    finally:
        database.close()

    assert default_result["seed"] == []
    assert {item["neighbor_id"] for item in expanded["seed"]} == {
        "memory-out",
        "memory-in",
        "evidence-out",
        "evidence-in",
    }


def test_expand_related_claims_filters_and_keeps_max_path_score(tmp_path) -> None:
    database = Database(tmp_path / "expansion.db")
    try:
        with database.connect() as connection:
            for claim_id, namespace, status in (
                ("seed", "default", "active"),
                ("eligible", "default", "active"),
                ("other-namespace", "other", "active"),
                ("retracted", "default", "retracted"),
                ("second-hop", "default", "active"),
            ):
                _insert_claim(connection, claim_id, namespace=namespace, status=status)
            connection.executemany(
                "INSERT INTO memory_relations(id,from_id,to_id,relation,confidence,evidence_json,created_at) "
                "VALUES (?,?,?,?,?,'[]',?)",
                [
                    ("m1", "seed", "eligible", "supports", 0.5, NOW),
                    ("m2", "eligible", "seed", "about", 1.0, NOW),
                    ("m3", "seed", "other-namespace", "supports", 1.0, NOW),
                    ("m4", "seed", "retracted", "supports", 1.0, NOW),
                    ("m5", "eligible", "second-hop", "supports", 1.0, NOW),
                    ("m6", "seed", "second-hop", "contradicts", 1.0, NOW),
                ],
            )
            connection.commit()

            claims, metadata = expand_related_claims(
                connection,
                ClaimRepository(connection),
                [{"id": "seed", "_semantic_score": 0.8}],
                NOW,
                None,
                RecallIntent.CURRENT_STATE,
                "default",
                RelationExpansionConfig(enabled=True),
            )
    finally:
        database.close()

    assert [claim["id"] for claim in claims] == ["eligible"]
    assert metadata[0].seed_id == "seed"
    assert metadata[0].relation == "about"
    assert metadata[0].expansion_score == 0.8 * 1.0 * 0.35 / 2


def test_relation_expansion_has_independent_candidate_budget(tmp_path) -> None:
    database = Database(tmp_path / "budget.db")
    try:
        with database.connect() as connection:
            _insert_claim(connection, "seed")
            for index in range(3):
                claim_id = f"neighbor-{index}"
                _insert_claim(connection, claim_id)
                connection.execute(
                    "INSERT INTO memory_relations(id,from_id,to_id,relation,confidence,evidence_json,created_at) "
                    "VALUES (?,?,?,?,?,'[]',?)",
                    (f"m{index}", "seed", claim_id, "supports", 1.0 - index * 0.1, NOW),
                )
            connection.commit()

            claims, _ = expand_related_claims(
                connection,
                ClaimRepository(connection),
                [{"id": "seed", "_semantic_score": 1.0}],
                NOW,
                None,
                RecallIntent.CURRENT_STATE,
                "default",
                RelationExpansionConfig(enabled=True, candidate_limit=2),
            )
    finally:
        database.close()

    assert [claim["id"] for claim in claims] == ["neighbor-0", "neighbor-1"]
