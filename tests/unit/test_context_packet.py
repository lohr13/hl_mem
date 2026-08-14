"""Context Packet v1 组装、严格 schema 与 exposure 原子性。"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pydantic import ValidationError

from hl_mem.api.schemas import (
    ContextPacketOutput,
    ContextPacketRecallOutput,
    RecallOutput,
)
from hl_mem.application.context_packet import (
    ContextPacketAssembler,
    RetrievalBundle,
    RetrievalBundleItem,
    pack_retrieval_bundle,
    pack_retrieval_items,
)
from hl_mem.mcp.server import get_tool_schemas
from hl_mem.storage.database import Database


def _id_factory(values: tuple[str, ...]) -> Iterator[str]:
    yield from values


def test_context_packet_v1_has_exact_shape_and_flat_exposure_ranks(tmp_path) -> None:
    connection = Database(tmp_path / "packet.db").open()
    identifiers = _id_factory(("feedback-1", "feedback-2"))
    bundle = RetrievalBundle(
        query_id="query-1",
        answerability="supported",
        items=(
            RetrievalBundleItem(
                "claim",
                "claim-1",
                "user likes tea",
                ({"type": "event", "id": "event-1"},),
                0.9,
            ),
            RetrievalBundleItem("observation", "observation-1", "tea preference is stable", (), 0.8),
        ),
        used_tokens_estimate=18,
        truncated=True,
    )

    packet = ContextPacketAssembler(
        connection,
        feedback_id_factory=lambda: next(identifiers),
        clock=lambda: "2026-07-31T00:00:00+00:00",
    ).assemble(bundle)

    assert set(packet) == {
        "schema_major",
        "schema_minor",
        "query_id",
        "answerability",
        "feedback_state",
        "items",
        "used_tokens_estimate",
        "truncated",
    }
    assert packet["feedback_state"] == "available"
    assert [set(item) for item in packet["items"]] == [
        {"type", "id", "text", "evidence", "feedback_id"},
        {"type", "id", "text", "evidence", "feedback_id"},
    ]
    assert [item["feedback_id"] for item in packet["items"]] == [
        "feedback-1",
        "feedback-2",
    ]
    rows = connection.execute(
        "SELECT id,rank,injected,helpful,task_outcome FROM retrieval_feedback ORDER BY rank"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("feedback-1", 1, 0, None, None),
        ("feedback-2", 2, 0, None, None),
    ]


def test_same_bundle_materializes_fresh_receipts_with_same_query_id(tmp_path) -> None:
    connection = Database(tmp_path / "fresh-receipts.db").open()
    identifiers = _id_factory(("feedback-a", "feedback-b"))
    bundle = RetrievalBundle(
        query_id="cached-query",
        answerability="low_confidence",
        items=(RetrievalBundleItem("claim", "claim-1", "project status unknown"),),
        used_tokens_estimate=11,
        truncated=False,
    )
    assembler = ContextPacketAssembler(
        connection,
        feedback_id_factory=lambda: next(identifiers),
        clock=lambda: "2026-07-31T00:00:00+00:00",
    )

    first = assembler.materialize(bundle)
    second = assembler.materialize(bundle)

    assert first["query_id"] == second["query_id"] == "cached-query"
    assert first["items"][0]["text"] == second["items"][0]["text"]
    assert first["items"][0]["feedback_id"] != second["items"][0]["feedback_id"]
    rows = connection.execute("SELECT query_id,id,injected FROM retrieval_feedback ORDER BY id").fetchall()
    assert [tuple(row) for row in rows] == [
        ("cached-query", "feedback-a", 0),
        ("cached-query", "feedback-b", 0),
    ]


def test_exposure_failure_rolls_back_batch_and_returns_degraded_packet(tmp_path) -> None:
    connection = Database(tmp_path / "degraded.db").open()
    connection.execute(
        "CREATE TRIGGER fail_second_exposure BEFORE INSERT ON retrieval_feedback "
        "WHEN NEW.memory_id='claim-2' BEGIN SELECT RAISE(ABORT, 'forced failure'); END"
    )
    identifiers = _id_factory(("feedback-1", "feedback-2"))
    bundle = RetrievalBundle(
        query_id="query-1",
        answerability="supported",
        items=(
            RetrievalBundleItem("claim", "claim-1", "first"),
            RetrievalBundleItem("claim", "claim-2", "second"),
        ),
        used_tokens_estimate=6,
        truncated=False,
    )

    packet = ContextPacketAssembler(
        connection,
        feedback_id_factory=lambda: next(identifiers),
    ).assemble(bundle)

    assert packet["feedback_state"] == "degraded"
    assert [item["feedback_id"] for item in packet["items"]] == [
        "feedback-1",
        "feedback-2",
    ]
    assert connection.execute("SELECT count(*) FROM retrieval_feedback").fetchone()[0] == 0


def test_packet_schema_forbids_extra_top_level_and_item_fields(tmp_path) -> None:
    connection = Database(tmp_path / "strict.db").open()
    packet = ContextPacketAssembler(
        connection,
        feedback_id_factory=lambda: "feedback-1",
    ).assemble(
        RetrievalBundle(
            query_id="query-1",
            answerability="supported",
            items=(RetrievalBundleItem("claim", "claim-1", "index text"),),
            used_tokens_estimate=5,
            truncated=False,
        )
    )

    ContextPacketOutput.model_validate(packet)
    with pytest.raises(ValidationError):
        ContextPacketOutput.model_validate({**packet, "packet_id": "forbidden"})
    invalid_item = {**packet, "items": [{**packet["items"][0], "rank": 1}]}
    with pytest.raises(ValidationError):
        ContextPacketOutput.model_validate(invalid_item)
    empty_feedback = {**packet, "items": [{**packet["items"][0], "feedback_id": ""}]}
    with pytest.raises(ValidationError):
        ContextPacketOutput.model_validate(empty_feedback)


def test_retrieval_bundle_rejects_hard_abstention_with_items() -> None:
    """hard abstention 若携带候选，reader 与 API 将收到互相矛盾的信号。"""
    with pytest.raises(ValueError, match="no_evidence"):
        RetrievalBundle(
            query_id="query-1",
            answerability="no_evidence",
            items=(RetrievalBundleItem("claim", "claim-1", "noise"),),
        )


def test_context_only_output_does_not_weaken_legacy_required_fields() -> None:
    legacy_schema = RecallOutput.model_json_schema()
    context_only_schema = ContextPacketRecallOutput.model_json_schema()

    assert {"results", "observations", "policies", "total"} <= set(legacy_schema["required"])
    assert context_only_schema["required"] == ["context_packet"]


def test_pack_retrieval_items_preserves_order_and_reports_budget_truncation() -> None:
    candidates = (
        RetrievalBundleItem("claim", "too-large", "x" * 20),
        RetrievalBundleItem("claim", "fits", "tea"),
        RetrievalBundleItem("claim", "also-fits", "ok"),
    )

    packed, used, truncated = pack_retrieval_items(candidates, 4)

    assert [item.id for item in packed] == ["fits", "also-fits"]
    assert used == 3
    assert truncated is True

    cacheable = RetrievalBundle("cached-query", "supported", candidates)
    finalized = pack_retrieval_bundle(cacheable, 4)
    assert cacheable.query_id == finalized.query_id == "cached-query"
    assert cacheable.used_tokens_estimate is None
    assert [item.id for item in finalized.items] == ["fits", "also-fits"]
    assert finalized.used_tokens_estimate == 3
    assert finalized.truncated is True


def test_mcp_recall_schema_freezes_packet_response_format_options() -> None:
    recall_tool = next(tool for tool in get_tool_schemas() if tool["name"] == "memory_recall")
    recall_schema = recall_tool["inputSchema"]

    assert recall_schema["properties"]["response_format"] == {
        "type": "string",
        "enum": ["legacy", "context_packet", "both"],
        "default": "legacy",
    }
    assert recall_schema["properties"]["token_budget"] == {
        "type": "integer",
        "minimum": 1,
    }
    packet_schema = recall_tool["outputSchema"]["properties"]["context_packet"]
    assert packet_schema["additionalProperties"] is False
    assert len(packet_schema["properties"]) == 8
    assert set(packet_schema["required"]) == set(packet_schema["properties"])
    item_schema = packet_schema["properties"]["items"]["items"]
    assert item_schema["additionalProperties"] is False
    assert len(item_schema["properties"]) == 5
    assert set(item_schema["required"]) == set(item_schema["properties"])
