"""关系候选发现与轻量关系图测试。"""

from __future__ import annotations

import sqlite3

import pytest

from hl_mem.domain.relations import walk_relation_graph
from hl_mem.llm.types import LLMResponse
from hl_mem.protocols import RelationProposal
from hl_mem.settings import Settings
from hl_mem.storage.database import Database
from hl_mem.workers.discover_relations import LLMRelationDiscoverer, build_neighbor_pool, discover_relations


class FakeRelationDiscoverer:
    """返回预设关系提案的测试发现器。"""

    def __init__(self, proposals: list[RelationProposal]) -> None:
        self.proposals = proposals

    def propose(self, source_claim, candidates, *, max_proposals):
        return self.proposals[:max_proposals]


class CapturingLLMClient:
    model = "fake-relation-model"

    def __init__(self) -> None:
        self.request = None

    def complete(self, request):
        self.request = request
        return LLMResponse(
            content={
                "relations": [
                    {
                        "from": "source",
                        "to": "target",
                        "relation": "supports",
                        "confidence": 0.97,
                        "rationale": "direct evidence",
                        "supporting_ids": [],
                    }
                ]
            },
            finish_reason="stop",
            usage_total_tokens=10,
        )


def test_llm_relation_prompt_freezes_fields_for_json_object_fallback() -> None:
    client = CapturingLLMClient()
    discoverer = LLMRelationDiscoverer(client)

    proposals = discoverer.propose(
        {"id": "source", "namespace_key": "default", "status": "active"},
        [{"id": "target", "namespace_key": "default", "status": "active"}],
        max_proposals=2,
    )

    assert proposals == [
        RelationProposal("source", "target", "supports", 0.97, "direct evidence", (), "fake-relation-model")
    ]
    assert client.request is not None
    system_prompt = client.request.messages[0].content
    assert '"relations"' in system_prompt
    assert all(f'"{field}"' in system_prompt for field in ("from", "to", "confidence", "rationale", "supporting_ids"))
    assert '"proposals"' not in system_prompt


def _insert_claim(
    connection: sqlite3.Connection,
    claim_id: str,
    namespace: str = "default",
    *,
    slot: str | None = None,
    tags: str = "[]",
    entities: str = "[]",
    subject: str | None = None,
    status: str = "active",
) -> None:
    connection.execute(
        "INSERT INTO claims(id,namespace_key,subject_entity_id,predicate,value_json,status,confidence,"
        "canonical_slot,topic_tags_json,entities_json,recorded_from) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            claim_id,
            namespace,
            subject,
            "p",
            '"v"',
            status,
            1.0,
            slot,
            tags,
            entities,
            "2026-01-01T00:00:00Z",
        ),
    )
    connection.commit()


@pytest.fixture
def connection(tmp_path):
    database = Database(tmp_path / "relations.db")
    result = database.open()
    yield result
    database.close()


def test_settings_default_keeps_relation_discovery_off() -> None:
    assert Settings().relation_discovery_mode == "off"


def test_neighbor_pool_is_namespace_isolated_prioritized_and_bounded(
    connection,
) -> None:
    _insert_claim(
        connection,
        "source",
        slot="choice.tool",
        tags='["python"]',
        entities='["project:hl"]',
        subject="user",
    )
    _insert_claim(connection, "slot", slot="choice.tool")
    _insert_claim(connection, "tag", tags='["python"]')
    _insert_claim(connection, "entity", entities='["project:hl"]')
    _insert_claim(connection, "subject", subject="user")
    _insert_claim(connection, "foreign", "other", slot="choice.tool")
    source = dict(connection.execute("SELECT * FROM claims WHERE id='source'").fetchone())
    source.update(topic_tags=["python"], entities=["project:hl"])
    pool = build_neighbor_pool(connection, source, 3)
    assert [item["id"] for item in pool] == ["slot", "tag", "entity"]
    assert all(item["namespace_key"] == "default" for item in pool)


def test_audit_records_proposal_without_writing_edge(connection) -> None:
    _insert_claim(connection, "source")
    _insert_claim(connection, "target")
    proposal = RelationProposal("source", "target", "supports", 0.99, "evidence", (), "fake")
    result = discover_relations(
        connection,
        FakeRelationDiscoverer([proposal]),
        "source",
        mode="audit",
        pool_limit=40,
        max_proposals=10,
        auto_apply_confidence=0.9,
        conflict_confidence=0.8,
    )
    assert result["proposals"] == 1
    assert connection.execute("SELECT count(*) FROM memory_relations").fetchone()[0] == 0
    assert connection.execute("SELECT status FROM relation_proposals").fetchone()[0] == "pending"


def test_auto_applies_high_confidence_safe_edge_and_rejects_summary(connection) -> None:
    _insert_claim(connection, "source")
    _insert_claim(connection, "target")
    proposals = [
        RelationProposal("source", "target", "supports", 0.95, "strong", (), "fake"),
        RelationProposal("target", "source", "summarizes", 0.99, "summary", (), "fake"),
    ]
    result = discover_relations(
        connection,
        FakeRelationDiscoverer(proposals),
        "source",
        mode="auto",
        pool_limit=40,
        max_proposals=10,
        auto_apply_confidence=0.9,
        conflict_confidence=0.8,
    )
    assert result["applied"] == 1
    assert result["rejected"] == 1
    assert connection.execute("SELECT count(*) FROM memory_relations").fetchone()[0] == 1


def test_contradiction_reuses_pair_case_and_marks_claims_disputed(connection) -> None:
    _insert_claim(connection, "source")
    _insert_claim(connection, "target")
    proposal = RelationProposal("source", "target", "contradicts", 0.9, "conflict", (), "fake")
    for _ in range(2):
        discover_relations(
            connection,
            FakeRelationDiscoverer([proposal]),
            "source",
            mode="auto",
            pool_limit=40,
            max_proposals=10,
            auto_apply_confidence=0.9,
            conflict_confidence=0.8,
        )
    assert connection.execute("SELECT count(*) FROM conflict_cases").fetchone()[0] == 1
    statuses = {row[0] for row in connection.execute("SELECT status FROM claims")}
    assert statuses == {"disputed"}


def test_recursive_graph_walk_stops_cycles_and_respects_namespace(connection) -> None:
    for claim_id in ("a", "b", "c"):
        _insert_claim(connection, claim_id)
    _insert_claim(connection, "foreign", "other")
    for relation_id, left, right in (
        ("ab", "a", "b"),
        ("bc", "b", "c"),
        ("ca", "c", "a"),
    ):
        connection.execute(
            "INSERT INTO memory_relations(id,from_id,to_id,relation,confidence,evidence_json,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (relation_id, left, right, "supports", 0.9, "[]", "2026-01-01T00:00:00Z"),
        )
    connection.commit()
    rows = walk_relation_graph(connection, ["a"], "default", max_depth=3, limit=20)
    assert {row["node_id"] for row in rows} == {"b", "c"}
    assert all("|a|a|" not in row["path"] for row in rows)
