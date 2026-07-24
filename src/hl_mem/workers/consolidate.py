"""异步语义冲突归并 worker。"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from hl_mem.config import CONSOLIDATE_GRAY_ZONE_MAX, CONSOLIDATE_GRAY_ZONE_MIN
from hl_mem.core.vector import cosine_similarity
from hl_mem.lifecycle import assert_transition
from hl_mem.llm.client import LLMClient
from hl_mem.llm.types import LLMMessage, LLMRequest, StructuredOutputMode, StructuredOutputSpec
from hl_mem.domain.claims.conflicts import compute_claim_pair_key
from hl_mem.storage.claims import ClaimRepository

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
        facts = {"left": {key: left.get(key) for key in fields}, "right": {key: right.get(key) for key in fields}}
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
                                "enum": ["contradiction", "compatible", "state_change", "unrelated"],
                            },
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            "rationale": {"type": "string"},
                            "current_claim_id": {"type": ["string", "null"]},
                        },
                        "required": ["kind", "confidence", "rationale", "current_claim_id"],
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
    try:
        hour_text, minute_text = cron.split(":", 1)
        scheduled_minutes = int(hour_text) * 60 + int(minute_text)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("HL_MEM_CONSOLIDATE_CRON must use HH:MM format") from error
    current = datetime.fromisoformat(now.replace("Z", "+00:00"))
    if not 0 <= scheduled_minutes < 24 * 60:
        raise ValueError("HL_MEM_CONSOLIDATE_CRON must use HH:MM format")
    if current.hour * 60 + current.minute < scheduled_minutes:
        return False
    from hl_mem.storage.jobs import JobRepository

    created = JobRepository(connection).insert_job(
        {
            "id": uuid.uuid4().hex,
            "job_type": "consolidate_conflicts",
            "payload_json": "{}",
            "idempotency_key": f"consolidate:{current.date().isoformat()}",
            "created_at": now,
            "updated_at": now,
        }
    )
    connection.commit()
    return created


class ConflictConsolidator:
    """扫描灰区相似 claim 并以幂等、CAS 方式应用判定。"""

    def __init__(self, connection: Any, judge: ConflictJudge, confidence_threshold: float = 0.8) -> None:
        self.connection = connection
        self.judge = judge
        self.confidence_threshold = confidence_threshold

    def scan_candidates(
        self, namespace: str = "default", watermark: str | None = None, batch_size: int = 100
    ) -> list[CandidatePair]:
        """生成同命名空间、同主题或事实槽的灰区候选。"""
        rows = ClaimRepository(self.connection).list_active_for_consolidation(namespace, watermark)
        pairs: list[CandidatePair] = []
        for index, left in enumerate(rows):
            for right in rows[index + 1 :]:
                same_slot = left.get("canonical_slot") and left.get("canonical_slot") == right.get("canonical_slot")
                if not same_slot and left.get("subject_entity_id") != right.get("subject_entity_id"):
                    continue
                similarity = cosine_similarity(left["embedding_dense"], right["embedding_dense"])
                if not CONSOLIDATE_GRAY_ZONE_MIN <= similarity < CONSOLIDATE_GRAY_ZONE_MAX:
                    continue
                pair_key = compute_claim_pair_key(left["id"], right["id"])
                signature = "|".join(sorted((left.get("embedding_model") or "", right.get("embedding_model") or "")))
                reviewed = self.connection.execute(
                    "SELECT 1 FROM consolidation_pairs WHERE pair_key=? AND embedding_signature=?",
                    (pair_key, signature),
                ).fetchone()
                if not reviewed:
                    pairs.append(CandidatePair(left, right, similarity, pair_key, signature))
                if len(pairs) >= batch_size:
                    return pairs
        return pairs

    def run_batch(
        self, limit: int = 100, namespace: str = "default", watermark: str | None = None, dry_run: bool = False
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
        for pair in self.scan_candidates(namespace, watermark, limit):
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

    def _record(self, pair: CandidatePair, decision: ConsolidationDecision, run_id: str, stored_decision: str) -> None:
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


def auto_resolve_conflicts(connection: Any, now: str) -> dict[str, int]:
    """自动解决低风险冲突，恢复来源权威性更高的 Claim。"""
    rows = connection.execute(
        "SELECT * FROM conflict_cases WHERE status='auto_resolved' AND resolved_at IS NULL"
    ).fetchall()
    repository = ClaimRepository(connection)
    resolved = 0
    deferred = 0
    for row in rows:
        case = dict(row)
        left = repository.get_claim(case["left_claim_id"])
        right = repository.get_claim(case["right_claim_id"])
        if not left or not right or left["status"] != "disputed" or right["status"] != "disputed":
            continue
        authority = {"high": 3, "medium": 2, "low": 1}
        left_score = authority.get(left.get("source_authority", "medium"), 2)
        right_score = authority.get(right.get("source_authority", "medium"), 2)
        if left_score == right_score:
            connection.execute(
                "UPDATE conflict_cases SET status='manual_required' WHERE id=?",
                (case["id"],),
            )
            deferred += 1
            continue
        winner_side = "left" if left_score > right_score else "right"
        winner_id = case[f"{winner_side}_claim_id"]
        assert_transition("disputed", "active")
        cursor = connection.execute(
            "UPDATE claims SET status='active' WHERE id=? AND status='disputed'",
            (winner_id,),
        )
        if cursor.rowcount != 1:
            continue
        connection.execute(
            "UPDATE conflict_cases SET status='resolved',resolved_at=?,decision=? WHERE id=?",
            (now, f"keep_{winner_side}", case["id"]),
        )
        resolved += 1
    connection.commit()
    return {"auto_resolved": resolved, "manual_required": deferred}
