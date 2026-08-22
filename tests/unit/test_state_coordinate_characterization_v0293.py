"""Freeze v0.29.3 state-coordinate failure behavior before v0.30.0 changes it."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from hl_mem.application.ingest import IngestService
from hl_mem.application.recall import RecallService
from hl_mem.domain.claims.temporal_links import evaluate_temporal_link
from hl_mem.domain.temporal import RecallIntent
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.recall.freshness_annotation import (
    FreshnessAnnotationPolicy,
    FreshnessItem,
    FreshnessRequest,
)
from hl_mem.recall.injection import InjectionContext
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database

OLD_TIME = "2026-08-20T08:00:00+00:00"
NEW_TIME = "2026-08-21T08:00:00+00:00"
RECALL_TIME = "2026-08-22T08:00:00+00:00"


def _store(
    connection: Any,
    *,
    event_id: str,
    occurred_at: str,
    subject: str,
    predicate: str,
    value: str,
    canonical_attribute: str,
    canonical_slot: str | None = None,
    qualifiers: dict[str, Any] | None = None,
) -> str:
    result = IngestService.store_extracted(
        connection,
        ExtractedClaim(
            predicate=predicate,
            value=value,
            subject=subject,
            canonical_attribute=canonical_attribute,
            canonical_slot=canonical_slot,
            qualifiers=qualifiers,
            assertion_kind="observation",
        ),
        {
            "id": event_id,
            "actor_type": "user",
            "tenant_id": "default",
            "occurred_at": occurred_at,
        },
        occurred_at,
        FakeEmbedder(8),
    )
    assert result.claim_id is not None
    return result.claim_id


def _recall_service(connection: Any, *, freshness_mode: str = "off") -> RecallService:
    settings = replace(
        Settings.for_test(),
        recall_dense_enabled=False,
        resurrection_mode="off",
        freshness_annotation_mode=freshness_mode,
    )
    return RecallService(connection, FakeEmbedder(8), settings=settings)


def test_version_shapes_follow_separate_identity_paths_and_stay_active(tmp_path: Any) -> None:
    """A future canonical state coordinate must make this characterization fail."""

    connection = Database(tmp_path / "coordinate-drift.db").open()
    claim_ids = [
        _store(
            connection,
            event_id="plain-version",
            occurred_at=OLD_TIME,
            subject="X",
            predicate="配置",
            value="X版本为0.1",
            canonical_attribute="config.version",
        ),
        _store(
            connection,
            event_id="running-version",
            occurred_at=NEW_TIME,
            subject="X",
            predicate="事实",
            value="X运行版本为0.1",
            canonical_attribute="fact.other",
        ),
        _store(
            connection,
            event_id="service-version",
            occurred_at=NEW_TIME,
            subject="X的8200服务",
            predicate="配置",
            value="X的8200服务版本为0.1",
            canonical_attribute="config.version",
        ),
    ]

    rows = [ClaimRepository(connection).get_claim(claim_id) for claim_id in claim_ids]
    identities = {(row["subject_entity_id"], row["predicate"], row["canonical_attribute"]) for row in rows}
    assert identities == {
        ("x", "配置", "config.version"),
        ("x", "事实", "fact.other"),
        ("x的8200服务", "配置", "config.version"),
    }
    assert len({row["legacy_conflict_key"] for row in rows}) == 3
    assert [row["conflict_key"] for row in rows] == [None, None, None]
    assert [row["status"] for row in rows] == ["active", "active", "active"]
    assert connection.execute("SELECT count(*) FROM conflict_cases").fetchone()[0] == 0


def test_config_version_has_no_conflict_key_while_exclusive_slot_does(tmp_path: Any) -> None:
    connection = Database(tmp_path / "version-slot.db").open()
    version_id = _store(
        connection,
        event_id="version",
        occurred_at=OLD_TIME,
        subject="X",
        predicate="配置",
        value="X版本为0.1",
        canonical_attribute="config.version",
    )
    port_id = _store(
        connection,
        event_id="port",
        occurred_at=OLD_TIME,
        subject="X",
        predicate="配置",
        value="X服务端口为8200",
        canonical_attribute="config.port",
        canonical_slot="config.port",
        qualifiers={"service": "X"},
    )

    repository = ClaimRepository(connection)
    version = repository.get_claim(version_id)
    port = repository.get_claim(port_id)
    assert (version["canonical_attribute"], version["canonical_slot"], version["conflict_key"]) == (
        "config.version",
        None,
        None,
    )
    assert (port["canonical_attribute"], port["canonical_slot"]) == ("config.port", "config.port")
    assert isinstance(port["conflict_key"], str) and len(port["conflict_key"]) == 16


def test_version_update_has_no_temporal_axis_and_both_values_remain_active(tmp_path: Any) -> None:
    existing = {
        "namespace_key": "default",
        "subject_entity_id": "x",
        "predicate": "配置",
        "canonical_attribute": "config.version",
        "canonical_slot": None,
        "assertion_kind": "observation",
        "qualifiers": {},
        "source_authority": "medium",
        "value": "X版本为0.1",
        "valid_from": OLD_TIME,
    }
    newer = {**existing, "value": "X版本为0.2", "valid_from": NEW_TIME}
    decision = evaluate_temporal_link(existing, newer)
    assert (decision.outcome, decision.rule_id, decision.rationale) == (
        "not_applicable",
        None,
        "no_proven_temporal_axis",
    )

    connection = Database(tmp_path / "version-temporal.db").open()
    old_id = _store(
        connection,
        event_id="version-old",
        occurred_at=OLD_TIME,
        subject="X",
        predicate="配置",
        value="X版本为0.1",
        canonical_attribute="config.version",
    )
    new_id = _store(
        connection,
        event_id="version-new",
        occurred_at=NEW_TIME,
        subject="X",
        predicate="配置",
        value="X版本为0.2",
        canonical_attribute="config.version",
    )
    old_row = ClaimRepository(connection).get_claim(old_id)
    new_row = ClaimRepository(connection).get_claim(new_id)
    assert (old_row["status"], old_row["valid_to"], old_row["superseded_by_id"]) == (
        "active",
        None,
        None,
    )
    assert (new_row["status"], new_row["supersedes_id"]) == ("active", None)


@pytest.mark.parametrize(
    ("old_value", "new_value", "expected"),
    [
        (
            "北方稀土2026年8月20日现价42.09元",
            "北方稀土2026年8月21日现价39.78元",
            ("snapshot_advance", "temporal-v1:snapshot-coordinate", "snapshot_coordinate_advanced"),
        ),
        (
            "北方稀土2026年8月20日现价42.09元",
            "北方稀土2026年8月21日收盘价39.78元",
            ("distinct_series", "temporal-v1:series-coordinate", "price_measure_differs:spot:close"),
        ),
        (
            "券商目标价42.09元",
            "券商目标价下调至39.78元",
            ("uncertain", "temporal-v1:explicit-price-replacement", "price_replacement_not_explicit"),
        ),
    ],
)
def test_price_snapshots_keep_the_existing_temporal_outcomes(
    old_value: str,
    new_value: str,
    expected: tuple[str, str, str],
) -> None:
    base = {
        "namespace_key": "default",
        "subject_entity_id": "北方稀土",
        "predicate": "事实",
        "canonical_attribute": "fact.other",
        "canonical_slot": None,
        "assertion_kind": "observation",
        "qualifiers": {},
        "source_authority": "medium",
    }
    decision = evaluate_temporal_link(
        {**base, "value": old_value, "valid_from": OLD_TIME},
        {**base, "value": new_value, "valid_from": NEW_TIME},
    )
    assert (decision.outcome, decision.rule_id, decision.rationale) == expected


def test_historical_bundle_text_omits_temporal_identity(tmp_path: Any) -> None:
    connection = Database(tmp_path / "historical-text.db").open()
    claim_id = _store(
        connection,
        event_id="historical-version",
        occurred_at=OLD_TIME,
        subject="X",
        predicate="配置",
        value="X版本为0.1",
        canonical_attribute="config.version",
    )
    stored = ClaimRepository(connection).get_claim(claim_id)

    response = _recall_service(connection).recall(
        "X版本",
        limit=5,
        as_of=RECALL_TIME,
        intent=RecallIntent.HISTORICAL,
        response_format="retrieval_bundle",
        token_budget=1000,
        ranking_now=RECALL_TIME,
    )
    [item] = response["retrieval_bundle"]["items"]
    assert item["id"] == claim_id
    assert item["text"] == stored["index_text"]
    assert set(item) == {"type", "id", "text", "evidence", "score"}
    assert OLD_TIME not in item["text"]
    assert "active" not in item["text"]


@pytest.mark.parametrize(
    ("intent", "as_of"),
    [
        (RecallIntent.HISTORICAL, None),
        (None, RECALL_TIME),
    ],
)
def test_historical_or_as_of_recall_records_access(
    tmp_path: Any,
    intent: RecallIntent | None,
    as_of: str | None,
) -> None:
    connection = Database(tmp_path / f"access-{intent or 'as-of'}.db").open()
    claim_id = _store(
        connection,
        event_id="access-version",
        occurred_at=OLD_TIME,
        subject="X",
        predicate="配置",
        value="X版本为0.1",
        canonical_attribute="config.version",
    )

    response = _recall_service(connection).recall(
        "X版本",
        limit=5,
        as_of=as_of,
        intent=intent,
        response_format="retrieval_bundle",
        token_budget=1000,
        ranking_now=RECALL_TIME,
    )
    assert [item["id"] for item in response["retrieval_bundle"]["items"]] == [claim_id]
    row = ClaimRepository(connection).get_claim(claim_id)
    assert row["access_count"] == 1
    assert row["last_accessed_at"] is not None


def test_freshness_bypasses_historical_and_only_annotates_current_state_age() -> None:
    policy = FreshnessAnnotationPolicy(mode="render")
    item = FreshnessItem(
        item_id="version",
        memory_type="claim",
        text="X版本为0.1",
        recorded_from=OLD_TIME,
        canonical_slot=None,
        canonical_attribute="config.version",
        topic_tags=("config", "version"),
    )
    historical = policy.evaluate(
        [item],
        FreshnessRequest(
            delivery_purpose="passive_injection",
            intent="historical",
            as_of=RECALL_TIME,
            known_as_of=None,
            rendering_now=RECALL_TIME,
        ),
    )
    current = policy.evaluate(
        [item],
        FreshnessRequest(
            delivery_purpose="passive_injection",
            intent="current_state",
            as_of=None,
            known_as_of=None,
            rendering_now=RECALL_TIME,
        ),
    )

    assert historical.bypass_reason == "historical_or_bitemporal"
    assert historical.decisions == ()
    [decision] = current.decisions
    assert (decision.eligible, decision.render_kind, decision.reason) == (True, "age_only", "eligible_age")
    assert decision.rendered_text.startswith("X版本为0.1\n【新鲜度：记录于 2 天前")
    assert decision.rendered_text.endswith("年龄不代表失效，执行前核验当前来源】")


def test_historical_freshness_bypass_does_not_filter_or_mutate_claim(tmp_path: Any) -> None:
    connection = Database(tmp_path / "historical-freshness.db").open()
    claim_id = _store(
        connection,
        event_id="freshness-version",
        occurred_at=OLD_TIME,
        subject="X",
        predicate="配置",
        value="X版本为0.1",
        canonical_attribute="config.version",
    )
    injection = InjectionContext.create(
        delivery_purpose="passive_injection",
        rendering_now=RECALL_TIME,
    )

    response = _recall_service(connection, freshness_mode="render").recall(
        "X版本",
        limit=5,
        as_of=RECALL_TIME,
        intent=RecallIntent.HISTORICAL,
        response_format="retrieval_bundle",
        token_budget=1000,
        ranking_now=RECALL_TIME,
        injection_context=injection,
    )
    [item] = response["retrieval_bundle"]["items"]
    row = ClaimRepository(connection).get_claim(claim_id)
    assert item["id"] == claim_id
    assert "新鲜度" not in item["text"]
    assert (row["status"], row["valid_to"]) == ("active", None)
