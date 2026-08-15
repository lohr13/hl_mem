from __future__ import annotations

import json
import sqlite3

import pytest

from hl_mem.evaluation.relation_semantics_ab import (
    SOURCE_FIRST_RELATION_OUTPUT_SCHEMA,
    SOURCE_FIRST_RELATION_SYSTEM_PROMPT,
    SourceFirstRelationDiscoverer,
    create_experiment_schema,
    overlay_packet_relations,
    persist_source_annotation,
    validate_source_annotation,
)
from hl_mem.llm.types import LLMResponse
from hl_mem.storage.database import Database


class CapturingClient:
    model = "fake-relation-model"

    def __init__(self, content: dict[str, object]) -> None:
        self.content = content
        self.request = None

    def complete(self, request):
        self.request = request
        return LLMResponse(
            content=self.content,
            finish_reason="stop",
            usage_total_tokens=30,
            input_tokens=20,
            output_tokens=10,
        )


def test_source_first_prompt_explicitly_requests_json_for_json_object_providers() -> None:
    assert "JSON" in SOURCE_FIRST_RELATION_SYSTEM_PROMPT


@pytest.fixture
def connection(tmp_path):
    database = Database(tmp_path / "relation-semantics.db")
    result = database.open()
    create_experiment_schema(result)
    yield result
    database.close()


def _insert_claim_with_evidence(
    connection: sqlite3.Connection,
    *,
    claim_id: str = "source",
    status: str = "active",
    value: str = "用户喜欢爵士乐",
    event_id: str = "event-1",
    event_text: str = "用户说：我喜欢爵士乐。",
) -> None:
    connection.execute(
        "INSERT INTO events(id,tenant_id,event_type,actor_type,content_json,occurred_at,recorded_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            event_id,
            "default",
            "message",
            "user",
            json.dumps({"text": event_text}, ensure_ascii=False),
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
        ),
    )
    connection.execute(
        "INSERT INTO claims(id,namespace_key,subject_entity_id,predicate,value_json,status,confidence,recorded_from) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            claim_id,
            "default",
            "用户",
            "fact",
            json.dumps(value, ensure_ascii=False),
            status,
            1.0,
            "2026-01-01T00:00:00Z",
        ),
    )
    connection.execute(
        "INSERT INTO evidence_links(id,derived_type,derived_id,evidence_type,evidence_id,relation,weight) "
        "VALUES (?,?,?,?,?,?,?)",
        (f"link-{claim_id}", "claim", claim_id, "event", event_id, "supports", 1.0),
    )
    connection.commit()


def test_source_first_contract_keeps_source_semantics_when_no_edge_exists() -> None:
    client = CapturingClient(
        {
            "source_semantics": {
                "claim_id": "source",
                "action": "喜欢",
                "object": "爵士乐",
                "evidence_event_id": "event-1",
                "evidence_quote": "我喜欢爵士乐",
            },
            "relations": [],
        }
    )
    discoverer = SourceFirstRelationDiscoverer(
        client,
        evidence_loader=lambda claim_id: [{"evidence_event_id": "event-1", "text": "用户说：我喜欢爵士乐。"}],
    )

    proposals = discoverer.propose(
        {
            "id": "source",
            "namespace_key": "default",
            "subject_entity_id": "用户",
            "predicate": "fact",
            "value": "用户喜欢爵士乐",
            "status": "active",
        },
        [
            {
                "id": "candidate",
                "namespace_key": "default",
                "subject_entity_id": "爵士乐",
                "predicate": "fact",
                "value": "爵士乐是音乐类型",
                "status": "active",
            }
        ],
        max_proposals=10,
    )

    assert proposals == []
    assert discoverer.last_source_semantics == {
        "claim_id": "source",
        "action": "喜欢",
        "object": "爵士乐",
        "evidence_event_id": "event-1",
        "evidence_quote": "我喜欢爵士乐",
    }
    assert discoverer.last_response_usage == {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}
    assert client.request is not None
    payload = json.loads(client.request.messages[1].content)
    assert payload["source"]["evidence"] == [{"evidence_event_id": "event-1", "text": "用户说：我喜欢爵士乐。"}]
    assert "evidence" not in payload["candidates"][0]
    assert SOURCE_FIRST_RELATION_OUTPUT_SCHEMA["required"] == ["source_semantics", "relations"]


def test_source_id_stays_authoritative_from_prompt_through_validator(connection) -> None:
    claim_id = "799fd998bf0649f0a0d2ff34a81e7980"
    _insert_claim_with_evidence(connection, claim_id=claim_id)
    client = CapturingClient(
        {
            "source_semantics": {
                "action": "喜欢",
                "object": "爵士乐",
                "evidence_event_id": "event-1",
                "evidence_quote": "我喜欢爵士乐",
            },
            "relations": [],
        }
    )
    discoverer = SourceFirstRelationDiscoverer(
        client,
        evidence_loader=lambda source_id: [{"evidence_event_id": "event-1", "text": "用户说：我喜欢爵士乐。"}],
    )

    discoverer.propose(
        {
            "id": claim_id,
            "namespace_key": "default",
            "subject_entity_id": "用户",
            "predicate": "fact",
            "value": "用户喜欢爵士乐",
            "status": "active",
        },
        [],
        max_proposals=10,
    )
    assert client.request is not None
    payload = json.loads(client.request.messages[1].content)
    assert payload["source"]["id"] == claim_id
    assert discoverer.last_source_semantics is not None
    assert discoverer.last_source_semantics["claim_id"] == claim_id

    validation = validate_source_annotation(connection, claim_id, discoverer.last_source_semantics)

    assert validation.reason == "accepted"
    assert validation.annotation is not None
    assert validation.annotation.claim_id == claim_id


def test_source_annotation_is_source_bounded_and_persists_no_quote_text(connection) -> None:
    _insert_claim_with_evidence(connection)
    raw = {
        "claim_id": "source",
        "action": "喜欢",
        "object": "爵士乐",
        "evidence_event_id": "event-1",
        "evidence_quote": "我喜欢爵士乐",
    }

    validation = validate_source_annotation(connection, "source", raw)

    assert validation.reason == "accepted"
    assert validation.annotation is not None
    assert (
        persist_source_annotation(
            connection,
            validation.annotation,
            model="fake-relation-model",
            prompt_sha256="a" * 64,
        )
        == "inserted"
    )
    row = dict(connection.execute("SELECT * FROM claim_relation_semantics").fetchone())
    assert row["claim_id"] == "source"
    assert row["action"] == "喜欢"
    assert row["object"] == "爵士乐"
    assert "evidence_quote" not in row
    assert row["evidence_quote_sha256"] != "我喜欢爵士乐"


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        ({"action": ""}, "missing_component"),
        ({"claim_id": "other"}, "source_id_mismatch"),
        ({"evidence_event_id": "missing"}, "unknown_evidence"),
        ({"action": "热爱"}, "action_not_in_evidence_quote"),
        ({"object": "摇滚乐"}, "object_not_in_evidence_quote"),
        ({"evidence_quote": "用户提到了爵士乐"}, "quote_not_in_evidence"),
    ],
)
def test_source_annotation_rejects_unprovable_components(connection, patch, reason) -> None:
    _insert_claim_with_evidence(connection)
    raw = {
        "claim_id": "source",
        "action": "喜欢",
        "object": "爵士乐",
        "evidence_event_id": "event-1",
        "evidence_quote": "我喜欢爵士乐",
        **patch,
    }

    result = validate_source_annotation(connection, "source", raw)

    assert result.annotation is None
    assert result.reason == reason


def test_packet_overlay_is_non_displacing_and_requires_active_claim(connection) -> None:
    _insert_claim_with_evidence(connection)
    validation = validate_source_annotation(
        connection,
        "source",
        {
            "claim_id": "source",
            "action": "喜欢",
            "object": "爵士乐",
            "evidence_event_id": "event-1",
            "evidence_quote": "我喜欢爵士乐",
        },
    )
    assert validation.annotation is not None
    persist_source_annotation(connection, validation.annotation, model="fake", prompt_sha256="b" * 64)
    packet = [
        {"claim_id": "source", "text": "用户喜欢爵士乐", "rendered_text": "用户喜欢爵士乐", "token_count": 4},
        {"claim_id": "other", "text": "保留顺序", "rendered_text": "保留顺序", "token_count": 4},
    ]

    overlaid, metrics = overlay_packet_relations(connection, packet, token_budget=100)

    assert [item["claim_id"] for item in overlaid] == ["source", "other"]
    assert [item["text"] for item in overlaid] == ["用户喜欢爵士乐", "保留顺序"]
    assert overlaid[0]["role"] == "用户"
    assert overlaid[0]["action"] == "喜欢"
    assert overlaid[0]["object"] == "爵士乐"
    assert overlaid[0]["rendered_text"] == "用户喜欢爵士乐\nrelation: 用户 → 喜欢 → 爵士乐"
    assert metrics["rendered"] == 1
    assert metrics["claim_ids_preserved"] is True

    connection.execute("UPDATE claims SET status='archived' WHERE id='source'")
    connection.commit()
    with pytest.raises(AssertionError, match="active"):
        overlay_packet_relations(connection, packet, token_budget=100)

    connection.execute("UPDATE claims SET status='active' WHERE id='source'")
    connection.commit()
    resurrected, _ = overlay_packet_relations(connection, packet, token_budget=100)
    assert resurrected[0]["action"] == "喜欢"


def test_packet_overlay_omits_relation_when_slack_is_insufficient(connection) -> None:
    _insert_claim_with_evidence(connection)
    validation = validate_source_annotation(
        connection,
        "source",
        {
            "claim_id": "source",
            "action": "喜欢",
            "object": "爵士乐",
            "evidence_event_id": "event-1",
            "evidence_quote": "我喜欢爵士乐",
        },
    )
    assert validation.annotation is not None
    persist_source_annotation(connection, validation.annotation, model="fake", prompt_sha256="c" * 64)
    packet = [{"claim_id": "source", "text": "用户喜欢爵士乐", "rendered_text": "用户喜欢爵士乐", "token_count": 9}]

    overlaid, metrics = overlay_packet_relations(connection, packet, token_budget=9)

    assert overlaid == packet
    assert metrics["rendered"] == 0
    assert metrics["omitted_for_budget"] == 1
