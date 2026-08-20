"""v0.29.3 temporal 回放与反例的 current-behavior characterization。

本文件只冻结批 0 时 ``evaluate_temporal_link`` 的真实输出。批 2 引入
``snapshot_advance`` 后，快照案应改钉三分支终态；R1-R4 反例则必须继续保持
人工处理/``uncertain``（R4 届时启用）。在此之前不要把这里记录的疑似误判当成
期望设计语义。
"""

from __future__ import annotations

from typing import Any

import pytest

from hl_mem.domain.claims.temporal_links import evaluate_temporal_link

PRICE_RULE = "temporal-v1:explicit-price-replacement"


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
        "uncertain",
        PRICE_RULE,
        "price_replacement_not_explicit",
        id="snapshot-01-northern-rare-earth-close",
    ),
    pytest.param(
        _claim("SKHY昨晚收盘价为160.18", "skhy", "2026-08-20T01:58:13Z"),
        _claim("SKHY隔夜收盘价为155.62", "skhy", "2026-08-19T02:07:17Z"),
        "uncertain",
        PRICE_RULE,
        "price_time_not_strictly_newer",
        id="snapshot-02-skhy-reverse-time",
    ),
    pytest.param(
        _claim("北稀现价41.20", "北稀", "2026-08-20T01:58:13Z"),
        _claim("北稀早盘最低价为40.98", "北稀", "2026-08-20T01:58:13Z"),
        "uncertain",
        PRICE_RULE,
        "price_time_not_strictly_newer",
        id="snapshot-03-same-time-mixed-measure",
    ),
    pytest.param(
        _claim("黄金ETF现价为8.997", "黄金etf", "2026-08-19T00:00:00Z"),
        _claim("2026年8月18日现价为9.081元", "黄金etf", "2026-08-18T00:00:00Z"),
        "uncertain",
        PRICE_RULE,
        "price_time_not_strictly_newer",
        id="snapshot-04-gold-etf-reverse-time",
    ),
    pytest.param(
        _claim("北方稀土现价为41.15", "北方稀土", "2026-08-19T00:00:00Z"),
        _claim("2026年8月18日最低价为42.05元", "北方稀土", "2026-08-18T00:00:00Z"),
        "uncertain",
        PRICE_RULE,
        "price_time_not_strictly_newer",
        id="snapshot-05-northern-rare-earth-reverse-time",
    ),
    pytest.param(
        _claim("北方稀土MA20价格为40.97", "北方稀土", "2026-08-18T02:09:47Z"),
        _claim("2026年8月18日收盘价为42.06元", "北方稀土", "2026-08-18T15:19:00Z"),
        "uncertain",
        PRICE_RULE,
        "price_replacement_not_explicit",
        id="snapshot-06-ma20-versus-close",
    ),
    pytest.param(
        _claim("持有1300股北方稀土，成本价42.2元", "user", "2026-08-18T15:19:00Z"),
        _claim(
            "持有117,900份创新药ETF 515120，成本价0.630元",
            "user",
            "2026-08-18T15:19:00Z",
        ),
        "uncertain",
        PRICE_RULE,
        "price_time_not_strictly_newer",
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
        PRICE_RULE,
        "price_time_not_strictly_newer",
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
        "uncertain",
        PRICE_RULE,
        "price_time_not_strictly_newer",
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
        "uncertain",
        PRICE_RULE,
        "price_time_not_strictly_newer",
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
        PRICE_RULE,
        "price_time_not_strictly_newer",
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
        "uncertain",
        PRICE_RULE,
        "price_time_not_strictly_newer",
        id="R3-mixed-measure-on-same-day",
    ),
]


@pytest.mark.parametrize(
    ("existing", "new", "expected_outcome", "expected_rule", "rationale_prefix"),
    COUNTEREXAMPLES,
)
def test_temporal_counterexamples_remain_manual_or_not_applicable(
    existing: dict[str, Any],
    new: dict[str, Any],
    expected_outcome: str,
    expected_rule: str | None,
    rationale_prefix: str,
) -> None:
    decision = evaluate_temporal_link(existing, new)

    assert (decision.outcome, decision.rule_id) == (expected_outcome, expected_rule)
    assert decision.rationale.startswith(rationale_prefix)


@pytest.mark.skip(reason="批2 snapshot_advance 语义后启用")
def test_r4_snapshot_advance_closes_old_current_state_validity() -> None:
    """旧价 current-state 快照届时必须被关闭 ``valid_to``。"""
