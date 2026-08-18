from __future__ import annotations

import pytest

from hl_mem.application.ingest import IngestService
from hl_mem.domain.claims.temporal_links import evaluate_temporal_link
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database


def _claim(
    value: str,
    *,
    claim_id: str,
    valid_from: str,
    assertion_kind: str = "unknown",
    canonical_attribute: str = "fact.other",
    canonical_slot: str | None = None,
    qualifiers: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "id": claim_id,
        "namespace_key": "default",
        "subject_entity_id": "deepseek-v4-flash",
        "predicate": "事实",
        "canonical_attribute": canonical_attribute,
        "canonical_slot": canonical_slot,
        "qualifiers": qualifiers or {},
        "value": value,
        "valid_from": valid_from,
        "recorded_from": valid_from,
        "assertion_kind": assertion_kind,
        "status": "active",
    }


def _extracted(
    value: str,
    *,
    subject: str = "deepseek-v4-flash",
    assertion_kind: str = "observation",
    canonical_attribute: str = "fact.other",
    canonical_slot: str | None = None,
    qualifiers: dict[str, object] | None = None,
) -> ExtractedClaim:
    return ExtractedClaim(
        predicate="事实" if not canonical_attribute.startswith("config.") else "配置",
        value=value,
        subject=subject,
        assertion_kind=assertion_kind,  # type: ignore[arg-type]
        canonical_attribute=canonical_attribute,
        canonical_slot=canonical_slot,
        qualifiers=qualifiers,
    )


def _store(connection, extracted: ExtractedClaim, event_id: str, occurred_at: str):
    return IngestService.store_extracted(
        connection,
        extracted,
        {
            "id": event_id,
            "actor_type": "user",
            "tenant_id": "default",
            "occurred_at": occurred_at,
        },
        occurred_at,
        FakeEmbedder(8),
    )


def test_explicit_observed_price_replacement_is_deterministic_state_change() -> None:
    old = _claim(
        "输入价格为 ¥1/百万 tokens",
        claim_id="old",
        valid_from="2026-08-09T00:00:00+00:00",
    )
    new = _claim(
        "输入新价：忙时¥3/闲时¥1.5每百万tokens，旧价¥1作废",
        claim_id="new",
        valid_from="2026-08-17T00:00:00+00:00",
        assertion_kind="observation",
    )

    decision = evaluate_temporal_link(old, new)

    assert (decision.outcome, decision.rule_id) == (
        "state_change",
        "temporal-v1:explicit-price-replacement",
    )


@pytest.mark.parametrize("assertion_kind", ["unknown", "inference"])
def test_untrusted_new_assertion_kind_cannot_authorize_temporal_link(assertion_kind: str) -> None:
    old = _claim(
        "输入价格为 ¥1/百万 tokens",
        claim_id="old",
        valid_from="2026-08-09T00:00:00+00:00",
    )
    new = _claim(
        "输入新价：忙时¥3/闲时¥1.5每百万tokens，旧价¥1作废",
        claim_id="new",
        valid_from="2026-08-17T00:00:00+00:00",
        assertion_kind=assertion_kind,
    )

    assert evaluate_temporal_link(old, new).outcome == "not_applicable"


@pytest.mark.parametrize("attribute", ["config.path", "config.network"])
def test_nonexclusive_operational_attributes_are_never_temporally_linked(attribute: str) -> None:
    old = _claim(
        "旧值 C:/one 作废",
        claim_id="old",
        valid_from="2026-08-09T00:00:00+00:00",
        canonical_attribute=attribute,
        canonical_slot=attribute,
        qualifiers={"target": "hl_mem"},
    )
    new = _claim(
        "新值 C:/two，旧值 C:/one 作废",
        claim_id="new",
        valid_from="2026-08-17T00:00:00+00:00",
        assertion_kind="observation",
        canonical_attribute=attribute,
        canonical_slot=attribute,
        qualifiers={"target": "hl_mem"},
    )

    assert evaluate_temporal_link(old, new).outcome == "not_applicable"


def test_newer_atomic_online_snapshot_closes_atomic_offline_snapshot() -> None:
    old = _claim(
        "小满当前处于离线状态",
        claim_id="old",
        valid_from="2026-08-16T08:10:21+00:00",
    )
    old["subject_entity_id"] = "小满"
    new = _claim(
        "在线",
        claim_id="new",
        valid_from="2026-08-17T08:27:42+00:00",
        assertion_kind="observation",
    )
    new["subject_entity_id"] = "小满"

    decision = evaluate_temporal_link(old, new)

    assert (decision.outcome, decision.rule_id) == (
        "state_change",
        "temporal-v1:atomic-availability",
    )


def test_compound_service_health_does_not_cross_atomic_availability_axis() -> None:
    old = _claim(
        "小满当前处于离线状态",
        claim_id="old",
        valid_from="2026-08-16T08:10:21+00:00",
    )
    old["subject_entity_id"] = "小满"
    new = _claim(
        "SSH 通（在线）",
        claim_id="new",
        valid_from="2026-08-17T08:27:42+00:00",
        assertion_kind="observation",
    )
    new["subject_entity_id"] = "小满"

    assert evaluate_temporal_link(old, new).outcome == "not_applicable"


def test_price_axis_qualifier_currency_and_time_boundaries_fail_closed() -> None:
    old = _claim(
        "输入价格为 ¥1/百万 tokens",
        claim_id="old",
        valid_from="2026-08-09T00:00:00+00:00",
        qualifiers={"model": "flash"},
    )
    base_new = _claim(
        "输入新价为 ¥3/百万 tokens，旧价 ¥1 作废",
        claim_id="new",
        valid_from="2026-08-17T00:00:00+00:00",
        assertion_kind="observation",
        qualifiers={"model": "flash"},
    )

    wrong_axis = dict(base_new, value="输出新价为 ¥3/百万 tokens，旧价 ¥1 作废")
    wrong_qualifier = dict(base_new, qualifiers={"model": "reasoner"})
    wrong_currency = dict(base_new, value="输入新价为 $3/百万 tokens，旧价 $1 作废")
    equal_time = dict(base_new, valid_from=old["valid_from"])

    assert evaluate_temporal_link(old, wrong_axis).outcome == "not_applicable"
    assert evaluate_temporal_link(old, wrong_qualifier).outcome == "not_applicable"
    assert evaluate_temporal_link(old, wrong_currency).outcome == "uncertain"
    assert evaluate_temporal_link(old, equal_time).outcome == "uncertain"

    high_authority_old = dict(old, source_authority="high")
    low_authority_new = dict(base_new, source_authority="low")
    assert evaluate_temporal_link(high_authority_old, low_authority_new).outcome == "uncertain"


def test_price_anchor_cannot_be_an_unrelated_shared_number() -> None:
    old = _claim(
        "套餐价格 100 元，包含 10 个用户",
        claim_id="old",
        valid_from="2026-08-09T00:00:00+00:00",
    )
    new = _claim(
        "套餐价格更新为 200 元，仍包含 10 个用户",
        claim_id="new",
        valid_from="2026-08-17T00:00:00+00:00",
        assertion_kind="observation",
    )

    decision = evaluate_temporal_link(old, new)

    assert (decision.outcome, decision.rationale) == ("uncertain", "old_price_not_anchored")


def test_price_anchor_cannot_select_an_unrelated_old_currency_amount() -> None:
    old = _claim(
        "套餐价格 100 元，附送 10 元优惠券",
        claim_id="old",
        valid_from="2026-08-09T00:00:00+00:00",
    )
    new = _claim(
        "套餐价格更新为 200 元，旧价 10 元作废",
        claim_id="new",
        valid_from="2026-08-17T00:00:00+00:00",
        assertion_kind="observation",
    )

    decision = evaluate_temporal_link(old, new)

    assert (decision.outcome, decision.rationale) == ("uncertain", "old_price_not_anchored")


def test_price_replacement_cannot_drop_known_currency_and_unit() -> None:
    old = _claim(
        "输入价格为 ¥1/百万 tokens",
        claim_id="old",
        valid_from="2026-08-09T00:00:00+00:00",
    )
    new = _claim(
        "输入价格更新为 3，旧价 1 作废",
        claim_id="new",
        valid_from="2026-08-17T00:00:00+00:00",
        assertion_kind="observation",
    )

    decision = evaluate_temporal_link(old, new)

    assert (decision.outcome, decision.rationale) == ("uncertain", "price_currency_or_unit_changed")


def test_ambiguous_price_update_enters_existing_manual_conflict_pipeline(tmp_path) -> None:
    connection = Database(tmp_path / "temporal-gray.db").open()
    old = _store(
        connection,
        _extracted("输入价格为 ¥1/百万 tokens", assertion_kind="unknown"),
        "old-event",
        "2026-08-09T00:00:00+00:00",
    )

    new = _store(
        connection,
        _extracted("输入价格现在是 ¥3/百万 tokens"),
        "new-event",
        "2026-08-17T00:00:00+00:00",
    )

    assert old.claim_id is not None and new.claim_id is not None
    assert {
        ClaimRepository(connection).get_claim(old.claim_id)["status"],
        ClaimRepository(connection).get_claim(new.claim_id)["status"],
    } == {"disputed"}
    case = connection.execute(
        "SELECT left_claim_id,right_claim_id,status,group_key,rationale FROM conflict_cases"
    ).fetchone()
    assert {case["left_claim_id"], case["right_claim_id"]} == {old.claim_id, new.claim_id}
    assert tuple(case[key] for key in ("status", "group_key", "rationale")) == (
        "manual_required",
        None,
        "temporal_update_uncertain",
    )


def test_explicit_price_replacement_supersedes_unknown_legacy_claim(tmp_path) -> None:
    connection = Database(tmp_path / "temporal-price.db").open()
    old = _store(
        connection,
        _extracted("输入价格为 ¥1/百万 tokens", assertion_kind="unknown"),
        "old-event",
        "2026-08-09T00:00:00+00:00",
    )

    new = _store(
        connection,
        _extracted("输入新价：忙时¥3/闲时¥1.5每百万tokens，旧价¥1作废"),
        "new-event",
        "2026-08-17T00:00:00+00:00",
    )

    assert old.claim_id is not None and new.claim_id is not None
    old_row = ClaimRepository(connection).get_claim(old.claim_id)
    new_row = ClaimRepository(connection).get_claim(new.claim_id)
    assert (old_row["status"], old_row["valid_to"]) == (
        "superseded",
        "2026-08-17T00:00:00+00:00",
    )
    assert (new_row["status"], new_row["supersedes_id"]) == ("active", old.claim_id)
    assert connection.execute("SELECT count(*) FROM conflict_cases").fetchone()[0] == 0


def test_unknown_default_preserves_legacy_coexistence(tmp_path) -> None:
    connection = Database(tmp_path / "temporal-unknown.db").open()
    old = _store(
        connection,
        _extracted("输入价格为 ¥1/百万 tokens", assertion_kind="unknown"),
        "old-event",
        "2026-08-09T00:00:00+00:00",
    )
    new = _store(
        connection,
        _extracted(
            "输入新价：忙时¥3/闲时¥1.5每百万tokens，旧价¥1作废",
            assertion_kind="unknown",
        ),
        "new-event",
        "2026-08-17T00:00:00+00:00",
    )

    assert old.claim_id is not None and new.claim_id is not None
    assert {row["status"] for row in connection.execute("SELECT status FROM claims")} == {"active"}
    assert connection.execute("SELECT count(*) FROM conflict_cases").fetchone()[0] == 0


def test_three_atomic_snapshots_converge_to_one_current_tip(tmp_path) -> None:
    connection = Database(tmp_path / "temporal-three-snapshots.db").open()
    first = _store(
        connection,
        _extracted("小满已离线 7 天", subject="小满", assertion_kind="unknown"),
        "offline-seven-days",
        "2026-08-16T06:47:22+00:00",
    )
    repeated = _store(
        connection,
        _extracted("小满当前处于离线状态", subject="小满"),
        "offline-current",
        "2026-08-16T08:10:21+00:00",
    )
    online = _store(
        connection,
        _extracted("在线", subject="小满"),
        "online-current",
        "2026-08-17T08:27:42+00:00",
    )

    assert first.claim_id is not None and online.claim_id is not None
    assert repeated.claim_id == first.claim_id
    rows = connection.execute(
        "SELECT id,status,value_json FROM claims WHERE subject_entity_id=? ORDER BY recorded_from",
        ("小满",),
    ).fetchall()
    assert [(row["id"], row["status"]) for row in rows] == [
        (first.claim_id, "superseded"),
        (online.claim_id, "active"),
    ]
    assert rows[1]["value_json"] == '"在线"'
    assert (
        connection.execute(
            "SELECT count(*) FROM evidence_links WHERE derived_id=? AND evidence_type='event'",
            (first.claim_id,),
        ).fetchone()[0]
        == 2
    )
