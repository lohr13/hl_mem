from hl_mem.recall.extended_pipeline import budget_pack, reciprocal_rank_fusion
from hl_mem.domain.recall import QueryRoute, route_query


def test_router_selects_temporal_relation_and_procedure_channels() -> None:
    assert route_query("去年使用什么数据库").intent == "historical"
    assert "temporal" in route_query("去年使用什么数据库").channels
    assert "relation" in route_query("项目 A 和用户有什么关系").channels
    procedure = route_query("如何部署服务")
    assert procedure.intent == "procedure"
    assert "procedure" in procedure.channels


def test_rrf_deduplicates_channels_and_budget_pack_is_deterministic() -> None:
    first = [{"id": "a", "text": "甲"}, {"id": "b", "text": "乙"}]
    second = [{"id": "b", "text": "乙"}, {"id": "c", "text": "丙"}]
    fused = reciprocal_rank_fusion([first, second], rank_constant=1)
    assert [item["id"] for item in fused] == ["b", "a", "c"]
    assert budget_pack(fused, token_budget=1) == [fused[0]]


def test_query_route_is_immutable_value_object() -> None:
    route = route_query("当前偏好")
    assert route == QueryRoute("current_state", ("fact", "fts", "dense"), None)
