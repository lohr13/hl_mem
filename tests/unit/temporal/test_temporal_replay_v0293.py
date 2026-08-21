"""v0.29.3 temporal 序列坐标的火山回放与 fail-closed 反例门禁。

R1 同坐标修订和 R2 隐式目标价替换保持人工；R3 混度量共存；R4 验证
``snapshot_advance`` 关闭旧 current tip，并由真实 recall 路径排除旧值。
"""

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
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database

PRICE_RULE = "temporal-v1:explicit-price-replacement"
SERIES_RULE = "temporal-v1:series-coordinate"
SNAPSHOT_RULE = "temporal-v1:snapshot-coordinate"


def _claim(
    value: str,
    subject: str,
    valid_from: str,
    *,
    recorded_from: str | None = None,
) -> dict[str, Any]:
    claim: dict[str, Any] = {
        "namespace_key": "default",
        "subject_entity_id": subject,
        "predicate": "事实",
        "canonical_attribute": "fact.other",
        "assertion_kind": "observation",
        "qualifiers": {},
        "source_authority": "medium",
        "value": value,
        "valid_from": valid_from,
    }
    if recorded_from is not None:
        claim["recorded_from"] = recorded_from
    return claim


REPLAY_CASES = [
    pytest.param(
        _claim("2026年8月18日现价为42.09元", "北方稀土", "2026-08-18T02:06:45Z"),
        _claim("北方稀土收盘价为39.78", "北方稀土", "2026-08-20T08:00:40Z"),
        "distinct_series",
        SERIES_RULE,
        "price_measure_differs",
        id="snapshot-01-northern-rare-earth-close",
    ),
    pytest.param(
        _claim("SKHY昨晚收盘价为160.18", "skhy", "2026-08-20T01:58:13Z"),
        _claim("SKHY隔夜收盘价为155.62", "skhy", "2026-08-19T02:07:17Z"),
        "snapshot_advance",
        SNAPSHOT_RULE,
        "snapshot_coordinate_precedes_existing",
        id="snapshot-02-skhy-reverse-time",
    ),
    pytest.param(
        _claim("北稀现价41.20", "北稀", "2026-08-20T01:58:13Z"),
        _claim("北稀早盘最低价为40.98", "北稀", "2026-08-20T01:58:13Z"),
        "distinct_series",
        SERIES_RULE,
        "price_measure_differs",
        id="snapshot-03-same-time-mixed-measure",
    ),
    pytest.param(
        _claim("黄金ETF现价为8.997", "黄金etf", "2026-08-19T00:00:00Z"),
        _claim("2026年8月18日现价为9.081元", "黄金etf", "2026-08-18T00:00:00Z"),
        "snapshot_advance",
        SNAPSHOT_RULE,
        "snapshot_coordinate_precedes_existing",
        id="snapshot-04-gold-etf-reverse-time",
    ),
    pytest.param(
        _claim("北方稀土现价为41.15", "北方稀土", "2026-08-19T00:00:00Z"),
        _claim("2026年8月18日最低价为42.05元", "北方稀土", "2026-08-18T00:00:00Z"),
        "distinct_series",
        SERIES_RULE,
        "price_measure_differs",
        id="snapshot-05-northern-rare-earth-reverse-time",
    ),
    pytest.param(
        _claim("北方稀土MA20价格为40.97", "北方稀土", "2026-08-18T02:09:47Z"),
        _claim("2026年8月18日收盘价为42.06元", "北方稀土", "2026-08-18T15:19:00Z"),
        "distinct_series",
        SERIES_RULE,
        "price_measure_differs",
        id="snapshot-06-ma20-versus-close",
    ),
    pytest.param(
        _claim("持有1300股北方稀土，成本价42.2元", "user", "2026-08-18T15:19:00Z"),
        _claim(
            "持有117,900份创新药ETF 515120，成本价0.630元",
            "user",
            "2026-08-18T15:19:00Z",
        ),
        "distinct_series",
        SERIES_RULE,
        "price_subject_differs",
        id="entity-07-unrelated-holdings",
    ),
    pytest.param(
        _claim(
            "用户的交易纪律是单一决策点为收盘价确认",
            "user",
            "2026-08-18T15:19:00Z",
        ),
        _claim(
            "对SKHY的既定纪律是收盘破$150清仓，否则持有",
            "user",
            "2026-08-18T15:19:00Z",
        ),
        "uncertain",
        SNAPSHOT_RULE,
        "price_subject_missing",
        id="entity-08-unrelated-trading-discipline",
    ),
    pytest.param(
        _claim(
            "明午计划切流量复测一次，若不通则花费 $8.79 换 IP",
            "user",
            "2026-08-18T15:19:00Z",
        ),
        _claim(
            "用户计划挂SKHY股价≤$150触发市价的条件单",
            "user",
            "2026-08-18T15:19:00Z",
        ),
        "distinct_series",
        SERIES_RULE,
        "price_measure_differs",
        id="entity-09-ip-plan-versus-order",
    ),
    pytest.param(
        _claim(
            "截至当前，Hermes 最新稳定版仍是 v0.20.1",
            "Hermes",
            "2026-08-18T15:19:00Z",
        ),
        _claim(
            "v0.28.7 版本增加了主动召回工具功能",
            "Hermes",
            "2026-08-18T15:19:00Z",
        ),
        "not_applicable",
        None,
        "no_proven_temporal_axis",
        id="entity-10-unrelated-version-facts",
    ),
    pytest.param(
        _claim(
            "若创新药价格破0.655则发出全清提示",
            "14:45尾盘确认任务",
            "2026-08-18T15:19:00Z",
        ),
        _claim(
            "若北方稀土价格≥41则持有，若<41则直接给出清仓指令",
            "14:45尾盘确认任务",
            "2026-08-18T15:19:00Z",
        ),
        "distinct_series",
        SERIES_RULE,
        "price_subject_differs",
        id="entity-11-unrelated-tail-confirmations",
    ),
]


@pytest.mark.parametrize(
    ("existing", "new", "expected_outcome", "expected_rule", "rationale_prefix"),
    REPLAY_CASES,
)
def test_volcano_replay_freezes_current_temporal_decisions(
    existing: dict[str, Any],
    new: dict[str, Any],
    expected_outcome: str,
    expected_rule: str | None,
    rationale_prefix: str,
) -> None:
    decision = evaluate_temporal_link(existing, new)

    assert (decision.outcome, decision.rule_id) == (expected_outcome, expected_rule)
    assert decision.rationale.startswith(rationale_prefix)


COUNTEREXAMPLES = [
    pytest.param(
        _claim(
            "北方稀土 8/20 收盘价 39.78",
            "北方稀土",
            "2026-08-20T15:00:00Z",
            recorded_from="2026-08-20T15:01:00Z",
        ),
        _claim(
            "北方稀土 8/20 收盘价 39.81，前值系复权口径误差",
            "北方稀土",
            "2026-08-20T15:00:00Z",
            recorded_from="2026-08-22T15:00:00Z",
        ),
        "uncertain",
        SNAPSHOT_RULE,
        "snapshot_coordinate_equal",
        id="R1-delayed-revision-at-same-coordinate",
    ),
    pytest.param(
        _claim("券商目标价 42.09", "北方稀土", "2026-08-18T00:00:00Z"),
        _claim("券商目标价下调至 39.78", "北方稀土", "2026-08-20T00:00:00Z"),
        "uncertain",
        PRICE_RULE,
        "price_replacement_not_explicit",
        id="R2-implicit-target-price-replacement",
    ),
    pytest.param(
        _claim("现价 42.09", "北方稀土", "2026-08-20T01:00:00Z"),
        _claim("收盘价 39.78", "北方稀土", "2026-08-20T01:00:00Z"),
        "distinct_series",
        SERIES_RULE,
        "price_measure_differs",
        id="R3-mixed-measure-on-same-day",
    ),
]


@pytest.mark.parametrize(
    ("existing", "new", "expected_outcome", "expected_rule", "rationale_prefix"),
    COUNTEREXAMPLES,
)
def test_temporal_counterexamples_match_fail_closed_boundaries(
    existing: dict[str, Any],
    new: dict[str, Any],
    expected_outcome: str,
    expected_rule: str | None,
    rationale_prefix: str,
) -> None:
    decision = evaluate_temporal_link(existing, new)

    assert (decision.outcome, decision.rule_id) == (expected_outcome, expected_rule)
    assert decision.rationale.startswith(rationale_prefix)


def _store_snapshot(connection: Any, value: str, event_id: str, occurred_at: str) -> str:
    result = IngestService.store_extracted(
        connection,
        ExtractedClaim(
            predicate="事实",
            value=value,
            subject="北方稀土",
            canonical_attribute="fact.other",
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


def test_r3_mixed_measure_snapshots_stay_active_without_manual_case(tmp_path: Any) -> None:
    connection = Database(tmp_path / "r3-distinct-series.db").open()
    spot_id = _store_snapshot(
        connection,
        "北方稀土 2026年8月20日现价 42.09元",
        "spot-event",
        "2026-08-20T10:00:00+00:00",
    )
    close_id = _store_snapshot(
        connection,
        "北方稀土 2026年8月20日收盘价 39.78元",
        "close-event",
        "2026-08-20T15:00:00+00:00",
    )

    rows = [ClaimRepository(connection).get_claim(claim_id) for claim_id in (spot_id, close_id)]
    assert [row["status"] for row in rows] == ["active", "active"]
    assert connection.execute("SELECT count(*) FROM conflict_cases").fetchone()[0] == 0


def test_r4_snapshot_advance_closes_old_current_state_validity(tmp_path: Any) -> None:
    """旧价必须退出 current-state FTS 召回，新价成为唯一 current tip。"""

    connection = Database(tmp_path / "r4-snapshot-advance.db").open()
    old_id = _store_snapshot(
        connection,
        "北方稀土 2026年8月18日现价 42.09元",
        "old-spot-event",
        "2026-08-18T10:00:00+00:00",
    )
    new_id = _store_snapshot(
        connection,
        "北方稀土 2026年8月20日现价 39.78元",
        "new-spot-event",
        "2026-08-20T10:00:00+00:00",
    )

    repository = ClaimRepository(connection)
    old_row = repository.get_claim(old_id)
    new_row = repository.get_claim(new_id)
    assert (old_row["status"], old_row["valid_to"], old_row["superseded_by_id"]) == (
        "superseded",
        "2026-08-20T10:00:00+00:00",
        new_id,
    )
    assert (new_row["status"], new_row["supersedes_id"]) == ("active", old_id)
    assert connection.execute("SELECT count(*) FROM conflict_cases").fetchone()[0] == 0

    service = RecallService(
        connection,
        FakeEmbedder(8),
        settings=replace(Settings.for_test(), recall_dense_enabled=False, resurrection_mode="off"),
    )
    current = service.recall(
        "北方稀土",
        limit=10,
        as_of="2026-08-21T00:00:00+00:00",
        intent=RecallIntent.CURRENT_STATE,
        response_format="retrieval_bundle",
        token_budget=1000,
        ranking_now="2026-08-21T00:00:00+00:00",
    )
    assert [item["id"] for item in current["retrieval_bundle"]["items"]] == [new_id]


def test_backfilled_snapshot_is_closed_by_existing_current_tip(tmp_path: Any) -> None:
    connection = Database(tmp_path / "snapshot-backfill.db").open()
    current_id = _store_snapshot(
        connection,
        "北方稀土 2026年8月20日现价 39.78元",
        "current-spot-event",
        "2026-08-20T10:00:00+00:00",
    )
    historical_id = _store_snapshot(
        connection,
        "北方稀土 2026年8月18日现价 42.09元",
        "historical-spot-event",
        "2026-08-18T10:00:00+00:00",
    )

    repository = ClaimRepository(connection)
    current = repository.get_claim(current_id)
    historical = repository.get_claim(historical_id)
    assert current["status"] == "active"
    assert (historical["status"], historical["valid_to"], historical["superseded_by_id"]) == (
        "superseded",
        "2026-08-20T10:00:00+00:00",
        current_id,
    )
    assert connection.execute("SELECT count(*) FROM conflict_cases").fetchone()[0] == 0
