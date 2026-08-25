from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from hl_mem.application.ingest import IngestService
from hl_mem.domain.instruments import InstrumentReference, resolve_instrument_target
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.storage.entities import EntityRepository

SH_MAOTAI = InstrumentReference(
    "instrument:CN:SH:600519",
    "CN:SH:600519",
    (("600519.SH", 1), ("SH600519", 1), ("600519", 3), ("贵州茅台", 2)),
)
SZ_COLLISION = InstrumentReference(
    "instrument:CN:SZ:600519",
    "CN:SZ:600519",
    (("600519.SZ", 1),),
)
NASDAQ_APPLE = InstrumentReference(
    "instrument:US:NASDAQ:AAPL",
    "US:NASDAQ:AAPL",
    (("NASDAQ:AAPL", 1), ("苹果公司", 2)),
)


def test_exchange_qualified_code_selects_one_existing_typed_instrument() -> None:
    resolution = resolve_instrument_target("贵州茅台 600519.SH 收盘价 1500 元", (SH_MAOTAI, SZ_COLLISION))

    assert resolution.outcome == "resolved"
    assert resolution.canonical_entity_id == "instrument:CN:SH:600519"
    assert resolution.mention == "600519.sh"
    assert resolution.alias_version == 1


def test_cn_six_digit_code_uses_exchange_prefix_rule() -> None:
    resolution = resolve_instrument_target("600519 当前价 1500 元", (SH_MAOTAI, SZ_COLLISION))

    assert resolution.outcome == "resolved"
    assert resolution.canonical_entity_id == "instrument:CN:SH:600519"


def test_bare_short_ticker_does_not_guess_market() -> None:
    resolution = resolve_instrument_target("AAPL 当前价 230 美元", (NASDAQ_APPLE,))

    assert resolution.outcome == "unresolved"
    assert resolution.canonical_entity_id is None


def test_company_name_requires_unique_typed_alias() -> None:
    duplicate = InstrumentReference(
        "instrument:HK:APPLE",
        "HK:APPLE",
        (("苹果公司", 1),),
    )

    resolution = resolve_instrument_target("苹果公司现价上涨", (NASDAQ_APPLE, duplicate))

    assert resolution.outcome == "ambiguous"
    assert resolution.canonical_entity_id is None


def test_multiple_distinct_instruments_in_one_claim_are_ambiguous() -> None:
    resolution = resolve_instrument_target(
        "比较 600519.SH 与 NASDAQ:AAPL 的价格",
        (SH_MAOTAI, NASDAQ_APPLE),
    )

    assert resolution.outcome == "ambiguous"


def _instrument_connection(tmp_path: Path, mode: str):
    settings = replace(Settings.for_test(), price_target_mode=mode, database_path=str(tmp_path / f"{mode}.db"))
    connection = Database(settings=settings).open()
    entities = EntityRepository(connection)
    entities.create_entity(
        "instrument:CN:SH:600519",
        "instrument",
        "CN:SH:600519",
        "贵州茅台",
        now="2026-08-25T09:00:00+00:00",
    )
    for alias in ("600519.SH", "贵州茅台"):
        entities.create_alias(
            alias,
            "instrument",
            "instrument:CN:SH:600519",
            "config_explicit",
            valid_from="2026-08-25T09:00:00+00:00",
        )
    connection.commit()
    return connection


def _store_price(connection, suffix: str):
    return IngestService.store_extracted(
        connection,
        ExtractedClaim(
            predicate="事实",
            value="贵州茅台 600519.SH 在 8月25日的收盘价为 1500 元",
            subject=f"价格记录-{suffix}",
            assertion_kind="observation",
        ),
        {"id": f"event-{suffix}", "tenant_id": "default", "actor_type": "user"},
        "2026-08-25T10:00:00+00:00",
        FakeEmbedder(8),
    )


def test_observe_target_resolution_does_not_mutate_claim(tmp_path: Path) -> None:
    connection = _instrument_connection(tmp_path, "observe")

    result = _store_price(connection, "observe")
    claim = ClaimRepository(connection).get_claim(str(result.claim_id))

    assert claim["canonical_target_entity_id"] is None
    assert (
        connection.execute(
            "SELECT count(*) FROM claim_entity_links WHERE claim_id=? AND role='target'",
            (result.claim_id,),
        ).fetchone()[0]
        == 0
    )


def test_enforce_target_resolution_dual_writes_claim_and_proof_link(tmp_path: Path) -> None:
    connection = _instrument_connection(tmp_path, "enforce")

    result = _store_price(connection, "enforce")
    claim = ClaimRepository(connection).get_claim(str(result.claim_id))
    link = connection.execute(
        "SELECT canonical_entity_id,role,mention_text,alias_version FROM claim_entity_links "
        "WHERE claim_id=? AND role='target'",
        (result.claim_id,),
    ).fetchone()

    assert claim["canonical_target_entity_id"] == "instrument:CN:SH:600519"
    assert tuple(link) == ("instrument:CN:SH:600519", "target", "600519.sh", 1)
