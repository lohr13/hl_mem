"""v0.29.3 ``store_extracted`` 事务与 audit 时序 characterization。

现有语义断言复用映射：

* exact duplicate 合并证据：``test_pipeline.py::test_fact_hash_exact_duplicate_merges_evidence``；
* conflict group 生命周期：``test_conflict_group_ingest.py``；
* temporal uncertain 人工复核：
  ``test_temporal_linking.py::test_ambiguous_price_update_enters_existing_manual_conflict_pipeline``。

本文件只补这些路径尚未冻结的 SQL 调用顺序，以及缓冲 audit 相对 COMMIT/ROLLBACK
的位置。代理仅记录生产代码显式发给 connection 的事务和写语句，不展开 SQLite
触发器内部语句。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from typing import Any

import pytest

from hl_mem.application.ingest import IngestService, StoreClaimResult
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.observability.audit import audit_scope
from hl_mem.storage.database import Database

NOW = "2026-08-21T12:00:00+00:00"


def _write_event(statement: str) -> str | None:
    normalized = " ".join(statement.strip().split())
    lowered = normalized.casefold()
    if lowered.startswith("begin"):
        return "BEGIN"
    insert = re.match(r"insert\s+(?:or\s+\w+\s+)?into\s+([a-z0-9_]+)", lowered)
    if insert is not None:
        return f"INSERT:{insert.group(1)}"
    update = re.match(r"update\s+([a-z0-9_]+)", lowered)
    if update is not None:
        return f"UPDATE:{update.group(1)}"
    return None


class _TracingConnection:
    """记录生产调用边界，同时把其余 sqlite API 透明委托给真实连接。"""

    def __init__(self, connection: sqlite3.Connection, timeline: list[str]) -> None:
        self._connection = connection
        self._timeline = timeline

    def execute(self, statement: str, parameters: Iterable[Any] = ()) -> sqlite3.Cursor:
        event = _write_event(statement)
        if event is not None:
            self._timeline.append(event)
        return self._connection.execute(statement, parameters)

    def executemany(self, statement: str, parameters: Iterable[Iterable[Any]]) -> sqlite3.Cursor:
        event = _write_event(statement)
        if event is not None:
            self._timeline.append(event)
        return self._connection.executemany(statement, parameters)

    def commit(self) -> None:
        self._connection.commit()
        self._timeline.append("COMMIT")

    def rollback(self) -> None:
        self._connection.rollback()
        self._timeline.append("ROLLBACK")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


class _TimelineAudit:
    def __init__(self, timeline: list[str]) -> None:
        self.timeline = timeline

    def emit(self, phase: str, action: str, outcome: str, **_kwargs: Any) -> bool:
        self.timeline.append(f"AUDIT:{phase}:{action}:{outcome}")
        return True


def _store(
    connection: sqlite3.Connection,
    extracted: ExtractedClaim,
    event_id: str,
    *,
    now: str = NOW,
) -> tuple[StoreClaimResult, list[str]]:
    timeline: list[str] = []
    traced = _TracingConnection(connection, timeline)
    with audit_scope(_TimelineAudit(timeline), event_id=event_id):
        result = IngestService.store_extracted(
            traced,  # type: ignore[arg-type]
            extracted,
            {
                "id": event_id,
                "actor_type": "user",
                "tenant_id": "default",
                "occurred_at": now,
            },
            now,
            FakeEmbedder(8),
        )
    return result, timeline


def _seed(
    connection: sqlite3.Connection,
    extracted: ExtractedClaim,
    event_id: str,
    *,
    now: str = NOW,
) -> StoreClaimResult:
    return IngestService.store_extracted(
        connection,
        extracted,
        {
            "id": event_id,
            "actor_type": "user",
            "tenant_id": "default",
            "occurred_at": now,
        },
        now,
        FakeEmbedder(8),
    )


def _assert_audit_after_commit(timeline: list[str]) -> None:
    commit_index = timeline.index("COMMIT")
    audit_indexes = [index for index, event in enumerate(timeline) if event.startswith("AUDIT:")]
    assert audit_indexes
    assert commit_index < min(audit_indexes)


def test_new_claim_commits_all_writes_before_flushing_audit_buffer(tmp_path: Any) -> None:
    database = Database(tmp_path / "new-claim-transaction.db")
    connection = database.open()

    result, timeline = _store(
        connection,
        ExtractedClaim(
            predicate="事实",
            value="用户偏好茉莉花茶",
            subject="user",
            canonical_attribute="fact.other",
            assertion_kind="observation",
        ),
        "event-new",
    )

    assert result.reason == "inserted"
    assert timeline[0] == "BEGIN"
    assert timeline[-4:] == [
        "COMMIT",
        "AUDIT:dedup:fact_hash_checked:new",
        "AUDIT:conflict:not_applicable:no_existing",
        "AUDIT:dedup:semantic_checked:new",
    ]
    for write in (
        "INSERT:canonical_entities",
        "INSERT:entity_aliases",
        "INSERT:claims",
        "INSERT:claims_fts_v2",
        "INSERT:claims_tags_fts_v2",
        "INSERT:evidence_links",
        "INSERT:claim_entity_links",
    ):
        assert write in timeline[1 : timeline.index("COMMIT")]
    _assert_audit_after_commit(timeline)
    database.close()


def test_exact_duplicate_early_return_commits_evidence_before_audit(tmp_path: Any) -> None:
    database = Database(tmp_path / "exact-duplicate-transaction.db")
    connection = database.open()
    extracted = ExtractedClaim(
        predicate="事实",
        value="用户偏好茉莉花茶",
        subject="user",
        canonical_attribute="fact.other",
        assertion_kind="observation",
    )
    first = _seed(connection, extracted, "event-original")

    duplicate, timeline = _store(connection, extracted, "event-duplicate")

    assert duplicate.claim_id == first.claim_id
    assert duplicate.reason == "exact_duplicate"
    assert timeline == [
        "BEGIN",
        "INSERT:evidence_links",
        "COMMIT",
        "AUDIT:dedup:fact_hash_checked:match",
    ]
    _assert_audit_after_commit(timeline)
    database.close()


def test_conflict_group_quarantine_is_atomic_and_audited_after_commit(tmp_path: Any) -> None:
    database = Database(tmp_path / "conflict-group-transaction.db")
    connection = database.open()
    old = ExtractedClaim(
        predicate="配置",
        value="8080",
        subject="gateway",
        qualifiers={"service": "gateway"},
        canonical_attribute="config.port",
        canonical_slot="config.port",
    )
    new = ExtractedClaim(
        predicate="配置",
        value="8081",
        subject="gateway",
        qualifiers={"service": "gateway"},
        canonical_attribute="config.port",
        canonical_slot="config.port",
    )
    first = _seed(connection, old, "event-port-8080")

    second, timeline = _store(connection, new, "event-port-8081")

    assert first.claim_id is not None and second.claim_id is not None
    assert timeline == [
        "BEGIN",
        "INSERT:claims",
        "INSERT:claims_fts_v2",
        "INSERT:claims_tags_fts_v2",
        "UPDATE:claims",
        "INSERT:conflict_cases",
        "INSERT:conflict_case_candidates",
        "INSERT:conflict_candidate_members",
        "UPDATE:conflict_case_candidates",
        "INSERT:conflict_case_candidates",
        "INSERT:conflict_candidate_members",
        "UPDATE:conflict_case_candidates",
        "UPDATE:conflict_cases",
        "INSERT:evidence_links",
        "COMMIT",
        "AUDIT:dedup:fact_hash_checked:new",
        "AUDIT:conflict:resolved:contradicts",
    ]
    _assert_audit_after_commit(timeline)
    statuses = {
        row["status"]
        for row in connection.execute(
            "SELECT status FROM claims WHERE id IN (?,?)",
            (first.claim_id, second.claim_id),
        )
    }
    assert statuses == {"disputed"}
    assert connection.execute("SELECT count(*) FROM conflict_cases").fetchone()[0] == 1
    database.close()


def test_temporal_uncertain_pair_quarantine_commits_case_before_audit(tmp_path: Any) -> None:
    database = Database(tmp_path / "temporal-uncertain-transaction.db")
    connection = database.open()
    first = _seed(
        connection,
        ExtractedClaim(
            predicate="事实",
            value="输入价格为 ¥1/百万 tokens",
            subject="deepseek-v4-flash",
            canonical_attribute="fact.other",
            assertion_kind="unknown",
        ),
        "event-old-price",
        now="2026-08-09T00:00:00+00:00",
    )

    second, timeline = _store(
        connection,
        ExtractedClaim(
            predicate="事实",
            value="输入价格现在是 ¥3/百万 tokens",
            subject="deepseek-v4-flash",
            canonical_attribute="fact.other",
            assertion_kind="observation",
        ),
        "event-new-price",
        now="2026-08-17T00:00:00+00:00",
    )

    assert first.claim_id is not None and second.claim_id is not None
    assert timeline == [
        "BEGIN",
        "INSERT:claims",
        "INSERT:claims_fts_v2",
        "INSERT:claims_tags_fts_v2",
        "UPDATE:claims",
        "INSERT:conflict_cases",
        "INSERT:evidence_links",
        "COMMIT",
        "AUDIT:dedup:fact_hash_checked:new",
        "AUDIT:conflict:temporal_link:uncertain",
    ]
    _assert_audit_after_commit(timeline)
    assert {
        row["status"]
        for row in connection.execute(
            "SELECT status FROM claims WHERE id IN (?,?)",
            (first.claim_id, second.claim_id),
        )
    } == {"disputed"}
    case = connection.execute("SELECT status,rationale FROM conflict_cases").fetchone()
    assert tuple(case) == (
        "manual_required",
        "temporal_update_uncertain:price_replacement_not_explicit",
    )
    database.close()


def test_failed_write_rolls_back_and_discards_buffered_audit(tmp_path: Any) -> None:
    database = Database(tmp_path / "rollback-transaction.db")
    connection = database.open()
    connection.execute(
        "CREATE TRIGGER fail_evidence BEFORE INSERT ON evidence_links "
        "BEGIN SELECT RAISE(ABORT, 'forced evidence failure'); END"
    )
    connection.commit()
    timeline: list[str] = []
    traced = _TracingConnection(connection, timeline)

    with audit_scope(_TimelineAudit(timeline), event_id="event-rollback"):
        with pytest.raises(sqlite3.IntegrityError, match="forced evidence failure"):
            IngestService.store_extracted(
                traced,  # type: ignore[arg-type]
                ExtractedClaim(
                    predicate="事实",
                    value="这条写入必须回滚",
                    subject="user",
                    canonical_attribute="fact.other",
                    assertion_kind="observation",
                ),
                {
                    "id": "event-rollback",
                    "actor_type": "user",
                    "tenant_id": "default",
                    "occurred_at": NOW,
                },
                NOW,
                FakeEmbedder(8),
            )

    assert timeline[0] == "BEGIN" and timeline[-1] == "ROLLBACK"
    assert "COMMIT" not in timeline
    for write in (
        "INSERT:canonical_entities",
        "INSERT:entity_aliases",
        "INSERT:claims",
        "INSERT:claims_fts_v2",
        "INSERT:claims_tags_fts_v2",
        "INSERT:evidence_links",
    ):
        assert write in timeline
    assert connection.execute("SELECT count(*) FROM claims").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM canonical_entities").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM entity_aliases").fetchone()[0] == 0
    assert not any(event.startswith("AUDIT:") for event in timeline)
    database.close()
