"""异步语义冲突归并 worker。"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

import httpx

from hl_mem.ingest.embeddings import cosine_similarity
from hl_mem.storage.repository import ClaimRepository
from hl_mem.lifecycle import assert_transition

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
    """使用兼容 OpenAI 的 JSON 接口判定语义冲突。"""

    def __init__(self, api_key: str, base_url: str, model: str, timeout: float | None = None) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout if timeout is not None else float(os.getenv("LLM_TIMEOUT", "90"))

    def judge(self, left: dict[str, Any], right: dict[str, Any]) -> ConsolidationDecision:
        """以严格 JSON 四分类判定 claim 对，失败最多重试三次。"""
        fields = (
            "id",
            "subject_entity_id",
            "canonical_attribute",
            "predicate",
            "value_json",
            "qualifiers_json",
            "valid_from",
            "valid_to",
            "source_authority",
        )
        facts = {"left": {key: left.get(key) for key in fields}, "right": {key: right.get(key) for key in fields}}
        payload = {
            "model": self.model,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": "将两条事实分类为 contradiction、compatible、state_change 或 unrelated。"
                    "仅输出 JSON：kind, confidence, rationale, current_claim_id。",
                },
                {"role": "user", "content": json.dumps(facts, ensure_ascii=False)},
            ],
        }
        for attempt in range(3):
            try:
                response = httpx.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                    timeout=self.timeout,
                )
                response.raise_for_status()
                raw = response.json()["choices"][0]["message"]["content"]
                data = raw if isinstance(raw, dict) else json.loads(raw)
                kind = data["kind"]
                if kind not in {"contradiction", "compatible", "state_change", "unrelated"}:
                    raise ValueError(f"invalid consolidation decision: {kind}")
                return ConsolidationDecision(
                    kind,
                    min(1.0, max(0.0, float(data["confidence"]))),
                    str(data.get("rationale", ""))[:512],
                    data.get("current_claim_id"),
                )
            except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                if attempt == 2:
                    raise
                time.sleep(2**attempt)
        raise RuntimeError("unreachable")


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
    from hl_mem.storage.repository import JobRepository

    return JobRepository(connection).insert_job(
        {
            "id": uuid.uuid4().hex,
            "job_type": "consolidate_conflicts",
            "payload_json": "{}",
            "idempotency_key": f"consolidate:{current.date().isoformat()}",
            "created_at": now,
            "updated_at": now,
        }
    )


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
        rows = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM claims WHERE namespace_key=? AND status='active' "
                "AND embedding_dense IS NOT NULL AND (? IS NULL OR recorded_from>?) "
                "ORDER BY recorded_from,id",
                (namespace, watermark, watermark),
            ).fetchall()
        ]
        pairs: list[CandidatePair] = []
        for index, left in enumerate(rows):
            for right in rows[index + 1 :]:
                same_slot = left.get("canonical_attribute") and left.get("canonical_attribute") == right.get(
                    "canonical_attribute"
                )
                if not same_slot and left.get("subject_entity_id") != right.get("subject_entity_id"):
                    continue
                similarity = cosine_similarity(left["embedding_dense"], right["embedding_dense"])
                if not 0.72 <= similarity < 0.95:
                    continue
                ids = sorted((left["id"], right["id"]))
                pair_key = hashlib.sha256("\0".join(ids).encode()).hexdigest()[:24]
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
                    json.loads(current["value_json"]),
                    current.get("valid_from") or current["recorded_from"],
                    datetime.now(timezone.utc).isoformat(),
                )
            self._record(pair, decision, run_id, decision.kind)
            self.connection.commit()
            stats["reviewed"] += 1
            stats[decision.kind] += 1
        return stats

    def _unchanged(self, pair: CandidatePair) -> bool:
        for original in (pair.left, pair.right):
            current = self.connection.execute(
                "SELECT status,value_json FROM claims WHERE id=?", (original["id"],)
            ).fetchone()
            if not current or current["status"] != "active" or current["value_json"] != original["value_json"]:
                return False
        return True

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
