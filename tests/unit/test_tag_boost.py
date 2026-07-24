"""topic_tags 检索加权与独立通道测试。"""

from __future__ import annotations

from typing import Any

from hl_mem.ingest.embedder import pack_vector
from hl_mem.recall.recall_pipeline import hybrid_claims

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

    def helpful_rates(self, _claim_ids: list[str]) -> dict[str, float]:
        return {}


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
        tag_channel_enabled=False,
    )

    assert [claim["id"] for claim in result] == ["first", "second"]
    assert all("_tag_boost" not in claim for claim in result)
    assert repo.tag_queries == []


def test_unrecognized_query_does_not_affect_ranking_or_search_tag_channel() -> None:
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
        tag_channel_enabled=True,
    )

    assert [claim["id"] for claim in result] == ["first", "second"]
    assert repo.tag_queries == []


def test_tag_channel_adds_candidate_and_ignores_empty_channel_in_denominator() -> None:
    """独立 tag channel 应补充候选，空通道则不稀释原有 RRF 分数。"""
    text_match = _claim("text")
    tag_match = _claim("tag", ["architecture"])
    with_tag = _Repo([text_match], [text_match], [tag_match])
    without_tag = _Repo([text_match], [text_match], [])

    added = hybrid_claims(
        with_tag,
        "架构",
        pack_vector([1.0]),
        2,
        None,
        now=NOW,
        tag_boost_enabled=False,
        tag_channel_enabled=True,
        tag_channel_weight=0.15,
        tag_candidate_limit=20,
    )
    baseline = hybrid_claims(
        without_tag,
        "架构",
        pack_vector([1.0]),
        1,
        None,
        now=NOW,
        tag_boost_enabled=False,
        tag_channel_enabled=True,
        tag_channel_weight=0.15,
        tag_candidate_limit=20,
    )

    assert {claim["id"] for claim in added} == {"text", "tag"}
    assert with_tag.tag_queries == [["architecture"]]
    assert baseline[0]["_score"] == hybrid_claims(
        without_tag,
        "架构",
        pack_vector([1.0]),
        1,
        None,
        now=NOW,
        tag_boost_enabled=False,
        tag_channel_enabled=False,
    )[0]["_score"]
