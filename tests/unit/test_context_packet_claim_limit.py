"""Context Packet claim-count budget regression tests."""

from hl_mem.application.context_packet import (
    ContextPacketAssembler,
    RetrievalBundle,
    RetrievalBundleItem,
    pack_retrieval_items,
)
from hl_mem.storage.database import Database


def test_pack_retrieval_items_caps_claims_at_ten_without_dropping_other_memory_types() -> None:
    candidates = tuple(RetrievalBundleItem("claim", f"claim-{index}", "x") for index in range(11)) + (
        RetrievalBundleItem("observation", "observation-1", "y"),
    )

    packed, used, truncated = pack_retrieval_items(candidates, 2_000)

    assert [item.id for item in packed] == [f"claim-{index}" for index in range(10)] + ["observation-1"]
    assert used == 11
    assert truncated is True


def test_context_packet_materialization_enforces_ten_claim_limit_without_explicit_token_budget(tmp_path) -> None:
    candidates = tuple(RetrievalBundleItem("claim", f"claim-{index}", "x") for index in range(11)) + (
        RetrievalBundleItem("observation", "observation-1", "y"),
    )
    assembler = ContextPacketAssembler(Database(tmp_path / "claim-limit.db").open())

    packet = assembler.assemble(RetrievalBundle("query-1", "supported", candidates))

    assert [item["id"] for item in packet["items"]] == [f"claim-{index}" for index in range(10)] + ["observation-1"]
    assert packet["truncated"] is True
