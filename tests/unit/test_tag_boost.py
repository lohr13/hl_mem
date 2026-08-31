"""topic_tags 检索加权与独立通道测试。"""

from __future__ import annotations

from typing import Any

import pytest

from hl_mem.ingest.embedder import pack_vector
from hl_mem.recall.recall_pipeline import RecallConfig, hybrid_claims
from hl_mem.recall.trace import SearchPhaseMetrics, SearchTrace, SearchTracer

NOW = "2026-07-24T00:00:00+00:00"


def _claim(claim_id: str, topic_tags: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": claim_id,
        "subject_entity_id": "project",
        "predicate": "记录",
        "value": claim_id,
        "topic_tags": topic_tags or [],
        "embedding_dense": pack_vector([1.0]),
        "status": "active",
        "valid_from": "2026-01-01T00:00:00+00:00",
        "valid_to": None,
        "recorded_from": "2026-01-01T00:00:00+00:00",
        "recorded_to": None,
        "confidence": 0.5,
        "importance": 0.5,
        "access_count": 0,
    }


class _Repo:
    def __init__(
        self,
        fts: list[dict[str, Any]],
        dense: list[dict[str, Any]],
        tags: list[dict[str, Any]] | None = None,
    ) -> None:
        self.fts = fts
        self.dense = dense
        self.tags = tags or []
        self.tag_queries: list[list[str]] = []

    def search_claims_fts(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return self.fts

    def search_claims_vector(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return self.dense

    def search_claims_tags(self, query_tags: list[str], *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        self.tag_queries.append(query_tags)
        return self.tags

    def helpful_rates(self, _claim_ids: list[str], _min_samples: int) -> dict[str, float]:
        return {}


def _tracer() -> SearchTracer:
    return SearchTracer(
        SearchTrace(
            query_id="query-1",
            query_hash="hash-only",
            intent="current_state",
            limit=2,
            candidate_limit=50,
            candidates={},
            phases=SearchPhaseMetrics(),
        )
    )


def test_recall_config_supplies_pipeline_defaults() -> None:
    first = _claim("first", ["architecture"])
    second = _claim("second")
    repo = _Repo([first, second], [first, second])

    result = hybrid_claims(
        repo,
        "架构",
        pack_vector([1.0]),
        2,
        None,
        now=NOW,
        recall_config=RecallConfig(
            candidate_floor=7,
            tag_boost_enabled=False,
            preference_recency_boost=0.12,
        ),
    )

    assert [claim["id"] for claim in result] == ["first", "second"]
    assert repo.tag_queries == []


def test_tag_boost_promotes_matching_candidate() -> None:
    """候选标签重叠应在 pre-rank 前提升匹配项。"""
    plain = _claim("plain")
    tagged = _claim("tagged", ["architecture"])
    repo = _Repo([plain, tagged], [plain, tagged])

    result = hybrid_claims(
        repo,
        "架构",
        pack_vector([1.0]),
        2,
        None,
        now=NOW,
        tag_boost_enabled=True,
        tag_boost_weight=0.05,
    )

    assert [claim["id"] for claim in result] == ["tagged", "plain"]
    assert result[0]["_tag_boost"] == 0.05
    assert "_tag_boost" not in result[1]


def test_slot_hint_boost_uses_legacy_canonical_attribute() -> None:
    """缺少 canonical_slot 的旧 claim 仍应通过 canonical_attribute 获得 slot boost。"""
    plain = _claim("plain")
    hardware = {**_claim("hardware"), "canonical_slot": None, "canonical_attribute": "config.hardware"}
    repo = _Repo([plain, hardware], [plain, hardware])
    tracer = _tracer()

    result = hybrid_claims(
        repo,
        "我的 GPU 是什么",
        pack_vector([1.0]),
        2,
        None,
        now=NOW,
        tracer=tracer,
    )

    assert [claim["id"] for claim in result] == ["hardware", "plain"]
    assert tracer.trace.query_slot_hints == ["config.hardware"]
    assert tracer.trace.slot_boost_applied is True


def test_tag_boost_disabled_preserves_existing_order_and_skips_tag_search() -> None:
    """两个功能关闭时不应改变既有排序或访问 tag channel。"""
    first = _claim("first", ["architecture"])
    second = _claim("second")
    repo = _Repo([first, second], [first, second])

    result = hybrid_claims(
        repo,
        "架构",
        pack_vector([1.0]),
        2,
        None,
        now=NOW,
        tag_boost_enabled=False,
    )

    assert [claim["id"] for claim in result] == ["first", "second"]
    assert all("_tag_boost" not in claim for claim in result)
    assert repo.tag_queries == []


def test_unrecognized_query_does_not_affect_ranking_or_search_tags() -> None:
    """无识别标签时即使 flags 开启也应完全跳过标签逻辑。"""
    first = _claim("first")
    second = _claim("second", ["architecture"])
    repo = _Repo([first, second], [first, second], [second])

    result = hybrid_claims(
        repo,
        "今天天气怎么样",
        pack_vector([1.0]),
        2,
        None,
        now=NOW,
        tag_boost_enabled=True,
    )

    assert [claim["id"] for claim in result] == ["first", "second"]
    assert repo.tag_queries == []


def test_tag_soft_boost_cannot_introduce_a_new_candidate() -> None:
    """Tag 只能重排 FTS/Dense 已有候选，不能独立补充候选。"""
    text_match = _claim("text")
    tag_match = _claim("tag", ["architecture"])
    repo = _Repo([text_match], [text_match], [tag_match])

    result = hybrid_claims(
        repo,
        "架构",
        pack_vector([1.0]),
        2,
        None,
        now=NOW,
        tag_boost_enabled=True,
    )

    assert [claim["id"] for claim in result] == ["text"]
    assert repo.tag_queries == []


def test_retired_tag_channel_arguments_are_not_accepted() -> None:
    claim = _claim("candidate", ["architecture"])

    with pytest.raises(TypeError, match="tag_channel_enabled"):
        hybrid_claims(
            _Repo([claim], [claim], [claim]),
            "架构",
            pack_vector([1.0]),
            1,
            None,
            now=NOW,
            tag_channel_enabled=True,
        )
