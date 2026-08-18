"""异步语义冲突归并 worker。"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal, Protocol

from hl_mem.application.conflict_invariants import (
    assert_conflict_case_postconditions,
    assert_global_conflict_postconditions,
)
from hl_mem.core.vector import cosine_similarity
from hl_mem.domain.claims.conflicts import compute_claim_pair_key, conflict_review_fingerprint
from hl_mem.domain.consolidation_scope import ConsolidationScope
from hl_mem.lifecycle import assert_transition
from hl_mem.llm.client import LLMClient
from hl_mem.llm.types import (
    LLMMessage,
    LLMRequest,
    StructuredOutputMode,
    StructuredOutputSpec,
)
from hl_mem.storage.claims import ClaimRepository
from hl_mem.workers.scheduling import enqueue_daily_job

DecisionKind = Literal["contradiction", "compatible", "state_change", "unrelated"]


@dataclass(frozen=True)
class ConsolidationDecision:
    """冲突判定结果。"""

    kind: DecisionKind
    confidence: float
    rationale: str
    current_claim_id: str | None = None


@dataclass(frozen=True)
class CandidatePair:
    """待判定 claim 对。"""

    left: dict[str, Any]
    right: dict[str, Any]
    similarity: float
    pair_key: str
    embedding_signature: str


class ConflictJudge(Protocol):
    """冲突分类器接口。"""

    def judge(self, left: dict[str, Any], right: dict[str, Any]) -> ConsolidationDecision: ...


class LLMConflictJudge:
    """通过统一 LLMClient 判定语义冲突。"""

    def __init__(self, llm_client: LLMClient) -> None:
        self.llm_client = llm_client

    def judge(self, left: dict[str, Any], right: dict[str, Any]) -> ConsolidationDecision:
        """以严格 JSON 四分类判定 claim 对，失败最多重试三次。"""
        fields = (
            "id",
            "subject_entity_id",
            "canonical_slot",
            "topic_tags",
            "predicate",
            "value",
            "qualifiers",
            "valid_from",
            "valid_to",
            "source_authority",
        )
        facts = {
            "left": {key: left.get(key) for key in fields},
            "right": {key: right.get(key) for key in fields},
        }
        response = self.llm_client.complete(
            LLMRequest(
                messages=[
                    LLMMessage(
                        role="system",
                        content="将两条事实分类为 contradiction、compatible、state_change 或 unrelated。"
                        "仅输出 JSON：kind, confidence, rationale, current_claim_id。",
                    ),
                    LLMMessage(role="user", content=json.dumps(facts, ensure_ascii=False)),
                ],
                structured_output=StructuredOutputSpec(
                    name="consolidation_decision",
                    schema={
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string",
                                "enum": [
                                    "contradiction",
                                    "compatible",
                                    "state_change",
                                    "unrelated",
                                ],
                            },
                            "confidence": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 1,
                            },
                            "rationale": {"type": "string"},
                            "current_claim_id": {"type": ["string", "null"]},
                        },
                        "required": [
                            "kind",
                            "confidence",
                            "rationale",
                            "current_claim_id",
                        ],
                        "additionalProperties": False,
                    },
                    preferred_mode=StructuredOutputMode.JSON_SCHEMA,
                ),
            )
        )
        data = response.content if isinstance(response.content, dict) else json.loads(response.content)
        kind = data["kind"]
        if kind not in {"contradiction", "compatible", "state_change", "unrelated"}:
            raise ValueError(f"invalid consolidation decision: {kind}")
        return ConsolidationDecision(
            kind,
            min(1.0, max(0.0, float(data["confidence"]))),
            str(data.get("rationale", ""))[:512],
            data.get("current_claim_id"),
        )


def enqueue_daily_consolidation(connection: Any, now: str, cron: str) -> bool:
    """到达本地计划时间后幂等创建当天的归并任务。"""
    return (
        enqueue_daily_job(
            connection,
            now,
            {"cron": cron, "idempotency_prefix": "consolidate"},
            "consolidate_conflicts",
            {},
            "HL_MEM_CONSOLIDATE_CRON",
        )
        is not None
    )


class ConflictConsolidator:
    """扫描灰区相似 claim 并以幂等、CAS 方式应用判定。"""

    def __init__(self, connection: Any, judge: ConflictJudge, confidence_threshold: float = 0.8) -> None:
        self.connection = connection
        self.judge = judge
        self.confidence_threshold = confidence_threshold

    def scan_candidates(
        self,
        namespace: str = "default",
        watermark: str | None = None,
        batch_size: int = 100,
        *,
        scope: ConsolidationScope | None = None,
    ) -> list[CandidatePair]:
        """生成同命名空间、同主题或事实槽的灰区候选。"""
        selected_scope = scope or ConsolidationScope(namespace=namespace, max_pairs=batch_size)
        rows = ClaimRepository(self.connection).list_active_for_consolidation(
            selected_scope.namespace,
            watermark,
            selected_scope.slot_filter,
            selected_scope.tag_filter,
        )
        pairs: list[CandidatePair] = []
        for index, left in enumerate(rows):
            for right in rows[index + 1 :]:
                same_slot = left.get("canonical_slot") and left.get("canonical_slot") == right.get("canonical_slot")
                if not same_slot and left.get("subject_entity_id") != right.get("subject_entity_id"):
                    continue
                similarity = cosine_similarity(left["embedding_dense"], right["embedding_dense"])
                if not selected_scope.similarity_threshold <= similarity < selected_scope.similarity_ceiling:
                    continue
                pair_key = compute_claim_pair_key(left["id"], right["id"])
                signature = "|".join(
                    sorted(
                        (
                            left.get("embedding_model") or "",
                            right.get("embedding_model") or "",
                        )
                    )
                )
                reviewed = self.connection.execute(
                    "SELECT 1 FROM consolidation_pairs WHERE pair_key=? AND embedding_signature=?",
                    (pair_key, signature),
                ).fetchone()
                if not reviewed:
                    pairs.append(CandidatePair(left, right, similarity, pair_key, signature))
                if len(pairs) >= selected_scope.max_pairs:
                    return pairs
        return pairs

    def run_batch(
        self,
        limit: int = 100,
        namespace: str = "default",
        watermark: str | None = None,
        dry_run: bool = False,
        progress_callback: Callable[[str, int, int], None] | None = None,
        scope: ConsolidationScope | None = None,
    ) -> dict[str, int]:
        """判定并处理一个候选批次。"""
        stats = {
            "reviewed": 0,
            "compatible": 0,
            "unrelated": 0,
            "contradiction": 0,
            "state_change": 0,
            "manual_review": 0,
            "cas_skipped": 0,
        }
        run_id = uuid.uuid4().hex
        candidates = self.scan_candidates(namespace, watermark, limit, scope=scope)
        total = len(candidates)
        for processed, pair in enumerate(candidates, start=1):
            if progress_callback is not None:
                progress_callback("review", processed, total)
            decision = self.judge.judge(pair.left, pair.right)
            if decision.confidence < self.confidence_threshold:
                stats["manual_review"] += 1
                self._record(pair, decision, run_id, "manual_review")
                continue
            if dry_run:
                stats[decision.kind] += 1
                continue
            if not self._unchanged(pair):
                stats["cas_skipped"] += 1
                continue
            if decision.kind == "contradiction":
                self.connection.execute("BEGIN IMMEDIATE")
                try:
                    current_rows = self.connection.execute(
                        "SELECT status FROM claims WHERE id IN (?,?)",
                        (pair.left["id"], pair.right["id"]),
                    ).fetchall()
                    if len(current_rows) != 2 or any(row["status"] != "active" for row in current_rows):
                        self.connection.rollback()
                        stats["cas_skipped"] += 1
                        continue
                    for row in current_rows:
                        assert_transition(row["status"], "disputed")
                    cursor = self.connection.execute(
                        "UPDATE claims SET status='disputed' WHERE id IN (?,?) AND status='active'",
                        (pair.left["id"], pair.right["id"]),
                    )
                    if cursor.rowcount != 2:
                        self.connection.rollback()
                        stats["cas_skipped"] += 1
                        continue
                    self.connection.execute(
                        "INSERT OR IGNORE INTO conflict_cases "
                        "(id,pair_key,left_claim_id,right_claim_id,status,decision,confidence,rationale,created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            uuid.uuid4().hex,
                            pair.pair_key,
                            pair.left["id"],
                            pair.right["id"],
                            "manual_required" if decision.confidence < 0.9 else "auto_resolved",
                            None,
                            decision.confidence,
                            decision.rationale,
                            datetime.now(timezone.utc).isoformat(),
                        ),
                    )
                except Exception:
                    self.connection.rollback()
                    raise
            elif decision.kind == "state_change":
                current_id = decision.current_claim_id
                if current_id not in {pair.left["id"], pair.right["id"]}:
                    stats["manual_review"] += 1
                    self._record(pair, decision, run_id, "manual_review")
                    continue
                current = pair.left if pair.left["id"] == current_id else pair.right
                old = pair.right if current is pair.left else pair.left
                ClaimRepository(self.connection).supersede_with_inline(
                    old["id"],
                    current["id"],
                    current["value"],
                    current.get("valid_from") or current["recorded_from"],
                    datetime.now(timezone.utc).isoformat(),
                )
            self._record(pair, decision, run_id, decision.kind)
            self.connection.commit()
            stats["reviewed"] += 1
            stats[decision.kind] += 1
        return stats

    def _unchanged(self, pair: CandidatePair) -> bool:
        repository = ClaimRepository(self.connection)
        return all(repository.is_unchanged(original) for original in (pair.left, pair.right))

    def _record(
        self,
        pair: CandidatePair,
        decision: ConsolidationDecision,
        run_id: str,
        stored_decision: str,
    ) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO consolidation_pairs(pair_key,embedding_signature,left_claim_id,"
            "right_claim_id,similarity,decision,confidence,rationale,run_id,reviewed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                pair.pair_key,
                pair.embedding_signature,
                pair.left["id"],
                pair.right["id"],
                pair.similarity,
                stored_decision,
                decision.confidence,
                decision.rationale,
                run_id,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.connection.commit()


_OPEN_CONFLICT_STATUSES_SQL = "('pending','auto_resolved','manual_required')"
_TERMINAL_CLAIM_STATUSES = {"expired", "retracted", "archived"}
_LIVING_CLAIM_STATUSES = {"active", "disputed", "candidate"}


def _follow_superseded_chain(repository: ClaimRepository, claim_id: str) -> dict[str, Any] | None:
    """追踪至非 superseded 端点；异常链返回 None。"""
    visited: set[str] = set()
    current_id = claim_id
    for redirect_count in range(33):
        if current_id in visited:
            return None
        visited.add(current_id)
        claim = repository.get_claim(current_id)
        if claim is None:
            return None
        if claim["status"] != "superseded":
            return claim
        next_id = claim.get("superseded_by_id")
        if not next_id or redirect_count == 32:
            return None
        current_id = str(next_id)
    return None


def _resolve_conflict_case(connection: Any, case_id: str, decision: str, now: str) -> None:
    cursor = connection.execute(
        "UPDATE conflict_cases SET status='resolved',resolved_at=?,decision=? "
        f"WHERE id=? AND status IN {_OPEN_CONFLICT_STATUSES_SQL} AND resolved_at IS NULL",
        (now, decision, case_id),
    )
    if cursor.rowcount != 1:
        raise RuntimeError(f"conflict case changed during resolution: {case_id}")


def _activate_uncontested_survivor(
    connection: Any,
    repository: ClaimRepository,
    case_id: str,
    survivor: dict[str, Any],
) -> bool:
    if survivor["status"] == "active":
        return False
    other_cases = connection.execute(
        "SELECT left_claim_id,right_claim_id FROM conflict_cases WHERE id<>? "
        f"AND status IN {_OPEN_CONFLICT_STATUSES_SQL} AND resolved_at IS NULL "
        "ORDER BY created_at,id",
        (case_id,),
    ).fetchall()
    for other_case in other_cases:
        for endpoint in (other_case["left_claim_id"], other_case["right_claim_id"]):
            tip = _follow_superseded_chain(repository, endpoint)
            if tip is None or tip["id"] == survivor["id"]:
                return False
    assert_transition(survivor["status"], "active")
    cursor = connection.execute(
        "UPDATE claims SET status='active' WHERE id=? AND status=?",
        (survivor["id"], survivor["status"]),
    )
    if cursor.rowcount != 1:
        raise RuntimeError(f"conflict survivor changed during resolution: {survivor['id']}")
    return True


def _ready_review_rows(
    connection: Any,
    now: str,
    *,
    max_cases: int,
) -> tuple[list[Any], int, int, Any | None]:
    ready_sql = (
        "state.dirty_at IS NOT NULL "
        "AND (state.not_before IS NULL OR state.not_before<=?) "
        f"AND cases.status IN {_OPEN_CONFLICT_STATUSES_SQL} AND cases.resolved_at IS NULL"
    )
    eligible = int(
        connection.execute(
            "SELECT count(*) FROM conflict_review_state AS state "
            "JOIN conflict_cases AS cases ON cases.id=state.case_id "
            f"WHERE {ready_sql}",
            (now,),
        ).fetchone()[0]
    )
    blocked = int(
        connection.execute(
            "SELECT count(*) FROM conflict_review_state AS state "
            "JOIN conflict_cases AS cases ON cases.id=state.case_id "
            "WHERE state.dirty_at IS NOT NULL AND state.not_before>? "
            f"AND cases.status IN {_OPEN_CONFLICT_STATUSES_SQL} AND cases.resolved_at IS NULL",
            (now,),
        ).fetchone()[0]
    )
    oldest = connection.execute(
        "SELECT min(state.dirty_at) FROM conflict_review_state AS state "
        "JOIN conflict_cases AS cases ON cases.id=state.case_id "
        f"WHERE {ready_sql}",
        (now,),
    ).fetchone()[0]
    if eligible == 0:
        return [], eligible, blocked, oldest

    cursor = connection.execute(
        "SELECT cursor_time,cursor_id FROM maintenance_cursors WHERE task='auto_resolve_conflicts'"
    ).fetchone()
    select_prefix = (
        "SELECT state.case_id,state.dirty_at,state.input_fingerprint,state.attempt_count "
        "FROM conflict_review_state AS state "
        "JOIN conflict_cases AS cases ON cases.id=state.case_id "
    )
    rows: list[Any] = []
    if cursor is not None and cursor["cursor_time"] is not None and cursor["cursor_id"] is not None:
        rows.extend(
            connection.execute(
                select_prefix
                + f"WHERE {ready_sql} AND (state.dirty_at,state.case_id)>(?,?) "
                "ORDER BY state.dirty_at,state.case_id LIMIT ?",
                (now, cursor["cursor_time"], cursor["cursor_id"], max_cases),
            ).fetchall()
        )
    remaining = max_cases - len(rows)
    if remaining > 0:
        seen = {str(row["case_id"]) for row in rows}
        wrapped = connection.execute(
            select_prefix + f"WHERE {ready_sql} ORDER BY state.dirty_at,state.case_id LIMIT ?",
            (now, max_cases),
        ).fetchall()
        rows.extend([row for row in wrapped if str(row["case_id"]) not in seen][:remaining])
    return rows, eligible, blocked, oldest


def _timestamp_after(now: str, seconds: int) -> str:
    parsed = datetime.fromisoformat(now.replace("Z", "+00:00"))
    return (parsed + timedelta(seconds=seconds)).isoformat()


def _record_review_failure(
    connection: Any,
    selected: Any,
    now: str,
    error: Exception,
    failure_backoff_seconds: int,
) -> None:
    next_attempt = int(selected["attempt_count"] or 0) + 1
    delay = min(3_600, failure_backoff_seconds * (2 ** min(next_attempt, 4)))
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "UPDATE conflict_review_state SET attempt_count=?,not_before=?,last_error=? "
            "WHERE case_id=? AND dirty_at IS NOT NULL",
            (
                next_attempt,
                _timestamp_after(now, delay),
                f"{type(error).__name__}: {str(error)[:512]}",
                selected["case_id"],
            ),
        )
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


def _clean_review_state(
    connection: Any,
    case_id: str,
    now: str,
    fingerprint: str,
    left_tip_id: str,
    right_tip_id: str,
) -> None:
    cursor = connection.execute(
        "UPDATE conflict_review_state SET dirty_at=NULL,dirty_reason='reviewed_clean',not_before=NULL,"
        "attempt_count=0,last_error=NULL,last_reviewed_at=?,input_fingerprint=?,left_tip_id=?,right_tip_id=? "
        "WHERE case_id=?",
        (now, fingerprint, left_tip_id, right_tip_id, case_id),
    )
    if cursor.rowcount != 1:
        raise RuntimeError(f"conflict review state changed during review: {case_id}")


def _review_conflict_case(connection: Any, selected: Any, now: str) -> dict[str, int]:
    repository = ClaimRepository(connection)
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT cases.*,state.input_fingerprint,state.dirty_at "
            "FROM conflict_cases AS cases "
            "JOIN conflict_review_state AS state ON state.case_id=cases.id "
            f"WHERE cases.id=? AND cases.status IN {_OPEN_CONFLICT_STATUSES_SQL} "
            "AND cases.resolved_at IS NULL AND state.dirty_at IS NOT NULL",
            (selected["case_id"],),
        ).fetchone()
        if row is None:
            connection.rollback()
            return {"changed": 0, "resolved": 0, "manual_stable": 0, "deferred": 1}
        case = dict(row)
        raw_left = repository.get_claim(str(case["left_claim_id"]))
        raw_right = repository.get_claim(str(case["right_claim_id"]))
        if raw_left is None or raw_right is None:
            raise RuntimeError(f"conflict case references missing claim: {case['id']}")
        left = _follow_superseded_chain(repository, str(case["left_claim_id"]))
        right = _follow_superseded_chain(repository, str(case["right_claim_id"]))
        if left is None or right is None:
            raise RuntimeError(f"conflict case has an invalid supersession chain: {case['id']}")

        touched_claim_ids: list[str] = []
        changed = 0
        resolved = 0
        manual_stable = 0
        deferred = 0

        if left["id"] == right["id"]:
            _resolve_conflict_case(connection, str(case["id"]), "obsolete", now)
            changed = resolved = 1
        else:
            left_terminal = left["status"] in _TERMINAL_CLAIM_STATUSES
            right_terminal = right["status"] in _TERMINAL_CLAIM_STATUSES
            if left_terminal and right_terminal:
                _resolve_conflict_case(connection, str(case["id"]), "obsolete", now)
                changed = resolved = 1
            elif left_terminal != right_terminal:
                survivor_side = "right" if left_terminal else "left"
                survivor = right if left_terminal else left
                if survivor["status"] not in _LIVING_CLAIM_STATUSES:
                    deferred = 1
                else:
                    activated = _activate_uncontested_survivor(
                        connection,
                        repository,
                        str(case["id"]),
                        survivor,
                    )
                    if activated:
                        touched_claim_ids.append(str(survivor["id"]))
                    _resolve_conflict_case(connection, str(case["id"]), f"keep_{survivor_side}", now)
                    changed = resolved = 1
            elif left["status"] != "disputed" or right["status"] != "disputed":
                if raw_left["status"] == "superseded" or raw_right["status"] == "superseded":
                    _resolve_conflict_case(connection, str(case["id"]), "obsolete", now)
                    changed = resolved = 1
                else:
                    deferred = 1
            else:
                authority = {"high": 3, "medium": 2, "low": 1}
                left_score = authority.get(left.get("source_authority", "medium"), 2)
                right_score = authority.get(right.get("source_authority", "medium"), 2)
                if left_score == right_score:
                    if case["status"] != "manual_required":
                        cursor = connection.execute(
                            "UPDATE conflict_cases SET status='manual_required' "
                            f"WHERE id=? AND status IN {_OPEN_CONFLICT_STATUSES_SQL} AND resolved_at IS NULL",
                            (case["id"],),
                        )
                        if cursor.rowcount != 1:
                            raise RuntimeError(f"conflict case changed during resolution: {case['id']}")
                        changed = 1
                        case["status"] = "manual_required"
                    manual_stable = deferred = 1
                else:
                    winner_side = "left" if left_score > right_score else "right"
                    winner = left if winner_side == "left" else right
                    loser = right if winner_side == "left" else left
                    assert_transition("disputed", "active")
                    assert_transition("disputed", "superseded")
                    winner_cursor = connection.execute(
                        "UPDATE claims SET status='active' WHERE id=? AND status='disputed'",
                        (winner["id"],),
                    )
                    if winner_cursor.rowcount != 1:
                        raise RuntimeError(f"conflict winner changed during resolution: {winner['id']}")
                    loser_cursor = connection.execute(
                        "UPDATE claims SET status='superseded',valid_to=?,recorded_to=?,superseded_by_id=? "
                        "WHERE id=? AND status='disputed'",
                        (now, now, winner["id"], loser["id"]),
                    )
                    if loser_cursor.rowcount != 1:
                        raise RuntimeError(f"conflict loser changed during resolution: {loser['id']}")
                    touched_claim_ids.extend((str(winner["id"]), str(loser["id"])))
                    _resolve_conflict_case(connection, str(case["id"]), f"keep_{winner_side}", now)
                    changed = resolved = 1

        if resolved == 0:
            fingerprint = conflict_review_fingerprint(case, left, right)
            _clean_review_state(
                connection,
                str(case["id"]),
                now,
                fingerprint,
                str(left["id"]),
                str(right["id"]),
            )
        namespace = str(left["namespace_key"]) if left.get("namespace_key") == right.get("namespace_key") else None
        conflict_key = str(left["conflict_key"]) if left.get("conflict_key") == right.get("conflict_key") else None
        assert_conflict_case_postconditions(
            connection,
            case_id=str(case["id"]),
            namespace=namespace,
            conflict_key=conflict_key,
            touched_claim_ids=touched_claim_ids,
        )
        connection.commit()
        return {
            "changed": changed,
            "resolved": resolved,
            "manual_stable": manual_stable,
            "deferred": deferred,
        }
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


def _update_conflict_cursor(connection: Any, dirty_at: str, case_id: str, now: str) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "INSERT INTO maintenance_cursors(task,cursor_time,cursor_id,updated_at) "
            "VALUES ('auto_resolve_conflicts',?,?,?) "
            "ON CONFLICT(task) DO UPDATE SET cursor_time=excluded.cursor_time,"
            "cursor_id=excluded.cursor_id,updated_at=excluded.updated_at",
            (dirty_at, case_id, now),
        )
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


def _oldest_dirty_age_seconds(oldest: str | None, now: str) -> float | None:
    if oldest is None:
        return None
    try:
        oldest_at = datetime.fromisoformat(str(oldest).replace("Z", "+00:00"))
        current = datetime.fromisoformat(now.replace("Z", "+00:00"))
        return max(0.0, (current - oldest_at).total_seconds())
    except ValueError:
        return None


def auto_resolve_conflicts(
    connection: Any,
    now: str,
    *,
    max_cases: int = 50,
    max_elapsed_ms: int = 1_000,
    failure_backoff_seconds: int = 300,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, int | float | str | bool | None]:
    """按持久 dirty 队列有界裁决；稳定 manual 不进入写事务。"""

    if not 1 <= max_cases <= 1_000:
        raise ValueError("max_cases must be between 1 and 1000")
    if not 1 <= max_elapsed_ms <= 10_000:
        raise ValueError("max_elapsed_ms must be between 1 and 10000")
    if not 1 <= failure_backoff_seconds <= 86_400:
        raise ValueError("failure_backoff_seconds must be between 1 and 86400")

    started = monotonic()
    rows, eligible, blocked, oldest = _ready_review_rows(connection, now, max_cases=max_cases)
    scanned = changed = resolved = manual_stable = deferred = failed = 0
    budget_exhausted = eligible > len(rows)
    last_cursor_time: str | None = None
    last_cursor_id: str | None = None
    for selected in rows:
        if scanned and (monotonic() - started) * 1_000 >= max_elapsed_ms:
            budget_exhausted = True
            break
        scanned += 1
        last_cursor_time = str(selected["dirty_at"])
        last_cursor_id = str(selected["case_id"])
        try:
            outcome = _review_conflict_case(connection, selected, now)
        except Exception as error:
            failed += 1
            deferred += 1
            _record_review_failure(connection, selected, now, error, failure_backoff_seconds)
            continue
        changed += outcome["changed"]
        resolved += outcome["resolved"]
        manual_stable += outcome["manual_stable"]
        deferred += outcome["deferred"]

    if scanned < eligible:
        budget_exhausted = True
    if last_cursor_time is not None and last_cursor_id is not None:
        _update_conflict_cursor(connection, last_cursor_time, last_cursor_id, now)
        assert_global_conflict_postconditions(connection)

    dirty_ready = int(
        connection.execute(
            "SELECT count(*) FROM conflict_review_state AS state "
            "JOIN conflict_cases AS cases ON cases.id=state.case_id "
            "WHERE state.dirty_at IS NOT NULL AND (state.not_before IS NULL OR state.not_before<=?) "
            f"AND cases.status IN {_OPEN_CONFLICT_STATUSES_SQL} AND cases.resolved_at IS NULL",
            (now,),
        ).fetchone()[0]
    )
    elapsed_ms = max(0, int((monotonic() - started) * 1_000))
    return {
        "eligible": eligible,
        "scanned": scanned,
        "changed": changed,
        "manual_stable": manual_stable,
        "resolved": resolved,
        "auto_resolved": resolved,
        "manual_required": manual_stable,
        "deferred": deferred,
        "failed": failed,
        "budget_exhausted": budget_exhausted,
        "cursor_time": last_cursor_time,
        "cursor_id": last_cursor_id,
        "elapsed_ms": elapsed_ms,
        "dirty_ready": dirty_ready,
        "dirty_blocked": blocked,
        "oldest_dirty_age_seconds": _oldest_dirty_age_seconds(oldest, now),
    }
