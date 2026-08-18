from __future__ import annotations

from dataclasses import replace

import pytest

from hl_mem.application.context_packet import (
    RetrievalBundle,
    RetrievalBundleItem,
    apply_freshness_decisions,
    estimate_tokens,
    pack_retrieval_bundle,
)
from hl_mem.application.recall import RecallService
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.recall.freshness_annotation import (
    FreshnessAnnotationPolicy,
    FreshnessItem,
    FreshnessRequest,
)
from hl_mem.recall.injection import InjectionContext
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database

NOW = "2026-08-18T12:00:00+00:00"


class _RecordingReranker:
    def __init__(self) -> None:
        self.documents: list[str] = []

    def rerank(self, _query: str, documents: list[str], top_n: int = 20) -> list[tuple[int, float]]:
        self.documents = list(documents)
        return [(index, 1.0 - index / 10) for index in range(min(top_n, len(documents)))]


def _request(**changes: object) -> FreshnessRequest:
    values = {
        "delivery_purpose": "passive_injection",
        "intent": "current_state",
        "as_of": None,
        "known_as_of": None,
        "rendering_now": NOW,
    }
    values.update(changes)
    return FreshnessRequest(**values)  # type: ignore[arg-type]


def _item(**changes: object) -> FreshnessItem:
    values = {
        "item_id": "claim-1",
        "memory_type": "claim",
        "text": "Use the current deployment procedure.",
        "recorded_from": "2026-08-18T06:00:00+00:00",
        "canonical_slot": "config.path",
        "canonical_attribute": "config.path",
        "topic_tags": (),
    }
    values.update(changes)
    return FreshnessItem(**values)  # type: ignore[arg-type]


def test_freshness_observe_reports_age_without_changing_bundle_text() -> None:
    policy = FreshnessAnnotationPolicy(mode="observe")
    evaluation = policy.evaluate([_item()], _request())
    decision = evaluation.decisions[0]
    bundle = RetrievalBundle(
        "query-1",
        "supported",
        (RetrievalBundleItem("claim", "claim-1", "Use the current deployment procedure."),),
    )

    unchanged = apply_freshness_decisions(bundle, evaluation)
    hypothetical = apply_freshness_decisions(bundle, evaluation, force_render=True)

    assert decision.eligible is True
    assert decision.render_kind == "age_only"
    assert decision.reason == "eligible_age"
    assert decision.rendered_text.endswith("【新鲜度：记录于 6 小时前；年龄不代表失效，执行前核验当前来源】")
    assert decision.added_token_estimate <= 18
    assert unchanged.items[0].text == bundle.items[0].text
    assert hypothetical.items[0].text == decision.rendered_text


@pytest.mark.parametrize(
    "stable_item",
    (
        _item(canonical_slot="preference.tool_choice", canonical_attribute="preference.tool_choice"),
        _item(canonical_slot="identity.role", canonical_attribute="identity.role"),
        _item(canonical_slot=None, canonical_attribute="memory.explicit"),
        _item(canonical_slot=None, canonical_attribute=None, topic_tags=("preference",)),
    ),
)
def test_freshness_tool_intent_excludes_stable_memory_classes(stable_item: FreshnessItem) -> None:
    evaluation = FreshnessAnnotationPolicy(mode="render").evaluate(
        [stable_item],
        _request(intent="tool"),
    )

    assert evaluation.decisions[0].eligible is False
    assert evaluation.decisions[0].reason == "skipped_stable"


def test_freshness_current_state_uses_positive_slot_or_tag_allowlist() -> None:
    items = [
        _item(item_id="slot", canonical_slot="state.service_health", topic_tags=()),
        _item(item_id="tag", canonical_slot=None, canonical_attribute=None, topic_tags=("dependency",)),
        _item(item_id="generic", canonical_slot=None, canonical_attribute=None, topic_tags=("fact",)),
    ]

    evaluation = FreshnessAnnotationPolicy(mode="render").evaluate(items, _request())

    assert {decision.item_id for decision in evaluation.decisions if decision.eligible} == {"slot", "tag"}
    assert evaluation.decisions[2].reason == "current_state_not_allowlisted"


@pytest.mark.parametrize(
    ("request_context", "reason"),
    (
        (_request(delivery_purpose="active_recall"), "non_passive_delivery"),
        (_request(delivery_purpose="api"), "non_passive_delivery"),
        (_request(intent="historical"), "historical_or_bitemporal"),
        (_request(as_of="2026-01-01T00:00:00+00:00"), "historical_or_bitemporal"),
        (_request(known_as_of="2026-01-01T00:00:00+00:00"), "historical_or_bitemporal"),
    ),
)
def test_freshness_non_passive_and_historical_paths_bypass(
    request_context: FreshnessRequest,
    reason: str,
) -> None:
    evaluation = FreshnessAnnotationPolicy(mode="render").evaluate([_item()], request_context)

    assert evaluation.decisions == ()
    assert evaluation.bypass_reason == reason


@pytest.mark.parametrize(
    ("recorded_from", "reason"),
    (
        ("not-a-time", "invalid_time"),
        ("2026-08-18T13:00:00+00:00", "future_time"),
    ),
)
def test_freshness_invalid_or_future_recorded_time_fails_open(recorded_from: str, reason: str) -> None:
    decision = (
        FreshnessAnnotationPolicy(mode="render")
        .evaluate(
            [_item(recorded_from=recorded_from)],
            _request(),
        )
        .decisions[0]
    )

    assert decision.eligible is False
    assert decision.reason == reason
    assert decision.rendered_text == _item(recorded_from=recorded_from).text


@pytest.mark.parametrize(
    ("recorded_from", "expected"),
    (
        ("2026-08-16T12:00:00+00:00", "记录于 2 天前"),
        ("2024-08-18T12:00:00+00:00", "【记录于 2024-08-18；执行前核验】"),
    ),
)
def test_freshness_formats_days_and_old_dates_with_fixed_token_cap(recorded_from: str, expected: str) -> None:
    decision = (
        FreshnessAnnotationPolicy(mode="render")
        .evaluate(
            [_item(recorded_from=recorded_from)],
            _request(),
        )
        .decisions[0]
    )

    assert expected in decision.rendered_text
    assert decision.added_token_estimate <= 18


def test_freshness_decoration_is_applied_before_final_packing() -> None:
    original = RetrievalBundle(
        "query-1",
        "supported",
        (
            RetrievalBundleItem("claim", "claim-1", "1234567890"),
            RetrievalBundleItem("observation", "observation-1", "ok"),
        ),
    )
    policy = FreshnessAnnotationPolicy(mode="render")
    evaluation = policy.evaluate(
        [_item(text="1234567890")],
        _request(),
    )
    decorated = apply_freshness_decisions(original, evaluation)
    budget = estimate_tokens("1234567890") + estimate_tokens("ok")

    control = pack_retrieval_bundle(original, budget)
    treatment = pack_retrieval_bundle(decorated, budget)

    assert [item.id for item in control.items] == ["claim-1", "observation-1"]
    assert [item.id for item in treatment.items] == ["observation-1"]
    assert treatment.used_tokens_estimate == estimate_tokens("ok")
    assert treatment.truncated is True


def test_freshness_prevents_duplicate_annotation() -> None:
    annotated = _item(text="procedure\n【新鲜度：记录于 6 小时前；年龄不代表失效，执行前核验当前来源】")
    decision = FreshnessAnnotationPolicy(mode="render").evaluate([annotated], _request()).decisions[0]

    assert decision.reason == "already_annotated"
    assert decision.rendered_text.count("【新鲜度：") == 1


def test_recall_service_decorates_before_packet_packing_and_traces_decision(tmp_path) -> None:
    database = Database(tmp_path / "freshness-recall.db")
    with database.connect() as connection:
        ClaimRepository(connection).insert_claim(
            {
                "id": "claim-1",
                "namespace_key": "default",
                "subject_entity_id": "hl_mem",
                "predicate": "fact",
                "value": "deployment uses editable source checkout",
                "index_text": "deployment uses editable source checkout",
                "canonical_attribute": "config.path",
                "canonical_slot": "config.path",
                "topic_tags_json": '["deployment"]',
                "scope": "permanent",
                "status": "active",
                "valid_from": "2026-01-01T00:00:00+00:00",
                "recorded_from": "2026-08-16T12:00:00+00:00",
                "confidence": 1.0,
                "importance": 0.8,
            }
        )
        ClaimRepository(connection).insert_claim(
            {
                "id": "claim-2",
                "namespace_key": "default",
                "subject_entity_id": "hl_mem",
                "predicate": "fact",
                "value": "deployment package uses PyPI install",
                "index_text": "deployment package uses PyPI install",
                "canonical_attribute": "config.path",
                "canonical_slot": "config.path",
                "topic_tags_json": '["deployment"]',
                "scope": "permanent",
                "status": "active",
                "valid_from": "2026-01-01T00:00:00+00:00",
                "recorded_from": "2026-08-16T12:00:00+00:00",
                "confidence": 1.0,
                "importance": 0.8,
            }
        )
        settings = replace(
            Settings(),
            freshness_annotation_mode="render",
            recall_dense_enabled=False,
            query_expansion_mode="off",
        )
        reranker = _RecordingReranker()
        service = RecallService(connection, FakeEmbedder(4), reranker=reranker, settings=settings)
        injection_context = InjectionContext.create(
            delivery_purpose="passive_injection",
            freshness_variant="render",
            rendering_now=NOW,
        )
        response = service.recall(
            "deployment",
            limit=2,
            debug=True,
            response_format="both",
            injection_context=injection_context,
        )
        procedure_response = service.recall(
            "deployment",
            limit=1,
            intent="procedure",
            debug=True,
            response_format="both",
            injection_context=injection_context,
        )
        packed_response = service.recall(
            "deployment",
            limit=2,
            context_mode="packed",
            injection_context=injection_context,
        )
    database.close()

    packet = response["context_packet"]
    rendered_text = packet["items"][0]["text"]
    assert rendered_text.endswith("【新鲜度：记录于 2 天前；年龄不代表失效，执行前核验当前来源】")
    assert packet["used_tokens_estimate"] == sum(estimate_tokens(item["text"]) for item in packet["items"])
    assert response["search_trace"]["injection"]["freshness_annotation"]["eligible"] == 2
    assert response["search_trace"]["injection"]["freshness_annotation"]["rendered"] == 2
    assert reranker.documents
    assert all("【新鲜度：" not in document for document in reranker.documents)
    assert procedure_response["context_packet"]["items"][0]["text"].endswith(
        "【新鲜度：记录于 2 天前；年龄不代表失效，执行前核验当前来源】"
    )
    assert procedure_response["search_trace"]["injection"]["freshness_annotation"]["rendered"] == 1
    assert all(
        item["data"]["text"].endswith("执行前核验当前来源】")
        for item in packed_response["context"]["context_items"]
        if item["type"] == "claim"
    )
