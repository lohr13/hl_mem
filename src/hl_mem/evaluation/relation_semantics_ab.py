"""v0.28 round-two source-first relation A/B support.

This module is imported only by evaluation runners.  It deliberately keeps
the seven-field product extractor and production relation-discovery path
unchanged until the frozen experiment passes every release gate.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from hl_mem.application.context_packet import estimate_tokens, render_memory_text
from hl_mem.ingest.llm_extractor import LLMExtractor
from hl_mem.llm.types import (
    LLMMessage,
    LLMRequest,
    StructuredOutputMode,
    StructuredOutputSpec,
)
from hl_mem.protocols import ClaimRow, RelationProposal
from hl_mem.workers.discover_relations import ALLOWED_RELATIONS

_COMPACT_RELATION_FIELDS = (
    "id",
    "subject_entity_id",
    "predicate",
    "value",
    "canonical_slot",
    "topic_tags",
    "entities",
)
_MAX_EVIDENCE_ITEMS = 2
_MAX_EVIDENCE_CHARS = 600
_MAX_ACTION_CHARS = 100
_MAX_OBJECT_CHARS = 500

_SOURCE_SEMANTICS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "claim_id": {"type": "string"},
        "action": {"type": "string", "minLength": 1, "maxLength": _MAX_ACTION_CHARS},
        "object": {"type": "string", "minLength": 1, "maxLength": _MAX_OBJECT_CHARS},
        "evidence_event_id": {"type": "string"},
        "evidence_quote": {"type": "string", "minLength": 1, "maxLength": _MAX_EVIDENCE_CHARS},
    },
    "required": ["claim_id", "action", "object", "evidence_event_id", "evidence_quote"],
    "additionalProperties": False,
}

_RELATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "from": {"type": "string"},
        "to": {"type": "string"},
        "relation": {"type": "string", "enum": sorted(ALLOWED_RELATIONS)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "rationale": {"type": "string"},
        "supporting_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["from", "to", "relation", "confidence", "rationale", "supporting_ids"],
    "additionalProperties": False,
}

SOURCE_FIRST_RELATION_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source_semantics": {"anyOf": [_SOURCE_SEMANTICS_SCHEMA, {"type": "null"}]},
        "relations": {"type": "array", "items": _RELATION_SCHEMA},
    },
    "required": ["source_semantics", "relations"],
    "additionalProperties": False,
}

SOURCE_FIRST_RELATION_SYSTEM_PROMPT = (
    "你是 source-first Claim 关系审计器。输入只包含已有 source、它的公开 evidence 和已有 candidates。"
    "只返回符合给定字段契约的 JSON 对象。"
    "第一步只为 source 提取一个可空的最小关系语义：action 必须是 source.value 与所引 evidence.text 中"
    "逐字共有的最小连续动词短语，object 必须是两处逐字共有的最小连续宾语；禁止同义改写、推断、"
    "跨 evidence 拼接或把整句当 object。没有完整且可证明的 action/object 时 source_semantics 必须为 null。"
    "claim_id/evidence_event_id 只能引用输入 ID。第二步只在 source 与 candidates 之间提出关系，"
    "关系仅限 about/follows/supports/contradicts/summarizes，不得创造 ID。"
    "必须只返回字段 source_semantics 与 relations；relations 的对象只能含 from、to、relation、confidence、"
    "rationale、supporting_ids。没有边时 relations 返回空数组，但 source_semantics 仍可非空。"
)


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


SOURCE_FIRST_RELATION_PROMPT_SHA256 = _canonical_hash(
    {"prompt": SOURCE_FIRST_RELATION_SYSTEM_PROMPT, "schema": SOURCE_FIRST_RELATION_OUTPUT_SCHEMA}
)


@dataclass(frozen=True, slots=True)
class ValidatedSourceAnnotation:
    claim_id: str
    action: str
    object: str
    evidence_event_id: str
    evidence_quote_sha256: str


@dataclass(frozen=True, slots=True)
class SourceAnnotationValidation:
    annotation: ValidatedSourceAnnotation | None
    reason: str


def _compact_claim(claim: Mapping[str, Any]) -> dict[str, Any]:
    return {key: claim.get(key) for key in _COMPACT_RELATION_FIELDS}


class SourceFirstRelationDiscoverer:
    """Evaluation-only discoverer that returns source semantics beside proposals."""

    def __init__(
        self,
        client: Any,
        *,
        evidence_loader: Callable[[str], Sequence[Mapping[str, Any]]],
    ) -> None:
        self.client = client
        self.evidence_loader = evidence_loader
        self.last_source_semantics: dict[str, Any] | None = None
        self.last_response_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        self.last_relations_empty = True

    def propose(
        self,
        source_claim: ClaimRow,
        candidates: list[ClaimRow],
        *,
        max_proposals: int,
    ) -> list[RelationProposal]:
        source_id = str(source_claim["id"])
        evidence = [
            {
                "evidence_event_id": str(item.get("evidence_event_id") or ""),
                "text": str(item.get("text") or "")[:_MAX_EVIDENCE_CHARS],
            }
            for item in self.evidence_loader(source_id)[:_MAX_EVIDENCE_ITEMS]
            if item.get("evidence_event_id") and item.get("text")
        ]
        source = {**_compact_claim(source_claim), "evidence": evidence}
        payload = {
            "source": source,
            "candidates": [_compact_claim(candidate) for candidate in candidates],
            "max_proposals": max_proposals,
        }
        response = self.client.complete(
            LLMRequest(
                messages=[
                    LLMMessage(role="system", content=SOURCE_FIRST_RELATION_SYSTEM_PROMPT),
                    LLMMessage(role="user", content=json.dumps(payload, ensure_ascii=False, default=str)),
                ],
                structured_output=StructuredOutputSpec(
                    name="source_first_relation_proposals",
                    schema=SOURCE_FIRST_RELATION_OUTPUT_SCHEMA,
                    preferred_mode=StructuredOutputMode.JSON_SCHEMA,
                ),
            )
        )
        decoded = response.content if isinstance(response.content, dict) else json.loads(response.content)
        raw_semantics = decoded.get("source_semantics")
        if isinstance(raw_semantics, Mapping):
            self.last_source_semantics = dict(raw_semantics)
            # The caller, not a non-strict JSON-object response, owns source identity.
            self.last_source_semantics["claim_id"] = source_id
        else:
            self.last_source_semantics = None
        self.last_response_usage = {
            "input_tokens": int(response.input_tokens or 0),
            "output_tokens": int(response.output_tokens or 0),
            "total_tokens": int(response.usage_total_tokens or 0),
        }
        relations = decoded.get("relations")
        self.last_relations_empty = not isinstance(relations, list) or not relations
        proposals: list[RelationProposal] = []
        for item in relations[:max_proposals] if isinstance(relations, list) else []:
            if not isinstance(item, Mapping):
                continue
            try:
                proposals.append(
                    RelationProposal(
                        from_claim_id=str(item["from"]),
                        to_claim_id=str(item["to"]),
                        relation=str(item["relation"]),
                        confidence=float(item["confidence"]),
                        rationale=str(item["rationale"]),
                        supporting_claim_ids=tuple(str(value) for value in item.get("supporting_ids", [])),
                        model=str(self.client.model),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return proposals


def create_experiment_schema(connection: sqlite3.Connection) -> None:
    """Create the reversible cache-local table; this is not a product migration."""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS claim_relation_semantics (
            claim_id TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            object TEXT NOT NULL,
            evidence_event_id TEXT NOT NULL,
            evidence_quote_sha256 TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (claim_id) REFERENCES claims(id),
            FOREIGN KEY (evidence_event_id) REFERENCES events(id)
        )
        """)
    connection.commit()


def _decode_public_value(raw: Any) -> str:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        value = raw
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) if value is not None else ""


def _public_event_text(raw: Any) -> str:
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return ""
    if isinstance(payload, Mapping):
        text = payload.get("text")
        return str(text) if isinstance(text, str) else ""
    return str(payload) if isinstance(payload, str) else ""


def load_source_evidence(connection: sqlite3.Connection, claim_id: str) -> list[dict[str, str]]:
    """Load only linked public event text for a source-first request."""
    rows = connection.execute(
        "SELECT event.id,event.content_json FROM evidence_links link "
        "JOIN events event ON event.id=link.evidence_id "
        "WHERE link.derived_type='claim' AND link.derived_id=? AND link.evidence_type='event' "
        "ORDER BY link.id,event.id LIMIT ?",
        (claim_id, _MAX_EVIDENCE_ITEMS),
    ).fetchall()
    return [
        {"evidence_event_id": str(row["id"]), "text": text[:_MAX_EVIDENCE_CHARS]}
        for row in rows
        if (text := _public_event_text(row["content_json"]))
    ]


def validate_source_annotation(
    connection: sqlite3.Connection,
    source_claim_id: str,
    raw: Mapping[str, Any] | None,
) -> SourceAnnotationValidation:
    """Validate IDs and source boundaries without consulting gold."""
    if raw is None:
        return SourceAnnotationValidation(None, "not_provided")
    claim_id = unicodedata.normalize("NFC", str(raw.get("claim_id") or "")).strip()
    if claim_id != source_claim_id:
        return SourceAnnotationValidation(None, "source_id_mismatch")
    action = unicodedata.normalize("NFC", str(raw.get("action") or "")).strip()
    object_ = unicodedata.normalize("NFC", str(raw.get("object") or "")).strip()
    event_id = unicodedata.normalize("NFC", str(raw.get("evidence_event_id") or "")).strip()
    quote = unicodedata.normalize("NFC", str(raw.get("evidence_quote") or "")).strip()
    if not action or not object_ or not event_id or not quote:
        return SourceAnnotationValidation(None, "missing_component")
    if len(action) > _MAX_ACTION_CHARS or len(object_) > _MAX_OBJECT_CHARS or len(quote) > _MAX_EVIDENCE_CHARS:
        return SourceAnnotationValidation(None, "overlong_span")

    claim = connection.execute(
        "SELECT subject_entity_id,value_json FROM claims WHERE id=?",
        (source_claim_id,),
    ).fetchone()
    if claim is None:
        return SourceAnnotationValidation(None, "missing_source")
    evidence = connection.execute(
        "SELECT event.content_json FROM evidence_links link "
        "JOIN events event ON event.id=link.evidence_id "
        "WHERE link.derived_type='claim' AND link.derived_id=? "
        "AND link.evidence_type='event' AND link.evidence_id=? LIMIT 1",
        (source_claim_id, event_id),
    ).fetchone()
    if evidence is None:
        return SourceAnnotationValidation(None, "unknown_evidence")
    public_evidence = unicodedata.normalize("NFC", _public_event_text(evidence["content_json"]))
    if quote not in public_evidence:
        return SourceAnnotationValidation(None, "quote_not_in_evidence")
    relation, reason = LLMExtractor._project_relation_metadata(
        subject=str(claim["subject_entity_id"] or ""),
        value=_decode_public_value(claim["value_json"]),
        evidence_quote=quote,
        action=action,
        object_=object_,
    )
    if reason != "accepted" or not relation:
        return SourceAnnotationValidation(None, reason)
    return SourceAnnotationValidation(
        ValidatedSourceAnnotation(
            claim_id=source_claim_id,
            action=action,
            object=object_,
            evidence_event_id=event_id,
            evidence_quote_sha256=hashlib.sha256(quote.encode("utf-8")).hexdigest(),
        ),
        "accepted",
    )


def persist_source_annotation(
    connection: sqlite3.Connection,
    annotation: ValidatedSourceAnnotation,
    *,
    model: str,
    prompt_sha256: str,
) -> str:
    """Insert once; exact replay is idempotent and divergent replay fails loud."""
    values = (
        annotation.claim_id,
        annotation.action,
        annotation.object,
        annotation.evidence_event_id,
        annotation.evidence_quote_sha256,
        model,
        prompt_sha256,
    )
    existing = connection.execute(
        "SELECT claim_id,action,object,evidence_event_id,evidence_quote_sha256,model,prompt_sha256 "
        "FROM claim_relation_semantics WHERE claim_id=?",
        (annotation.claim_id,),
    ).fetchone()
    if existing is not None:
        if tuple(existing) == values:
            return "reused"
        raise RuntimeError(f"divergent source annotation replay for claim {annotation.claim_id}")
    connection.execute(
        "INSERT INTO claim_relation_semantics("
        "claim_id,action,object,evidence_event_id,evidence_quote_sha256,model,prompt_sha256,created_at"
        ") VALUES (?,?,?,?,?,?,?,?)",
        (*values, datetime.now(timezone.utc).isoformat()),
    )
    connection.commit()
    return "inserted"


def overlay_packet_relations(
    connection: sqlite3.Connection,
    packet: Sequence[Mapping[str, Any]],
    *,
    token_budget: int,
) -> tuple[list[dict[str, Any]], dict[str, int | bool]]:
    """Append source RAO only from remaining slack; never displace a claim."""
    overlaid = [dict(item) for item in packet]
    original_ids = [str(item.get("claim_id") or item.get("id") or "") for item in packet]
    used = sum(
        int(item.get("token_count") or estimate_tokens(str(item.get("rendered_text") or item.get("text") or "")))
        for item in overlaid
    )
    metrics: dict[str, int | bool] = {
        "available": 0,
        "rendered": 0,
        "omitted_for_budget": 0,
        "token_overhead": 0,
        "claim_ids_preserved": True,
    }
    for item in overlaid:
        claim_id = str(item.get("claim_id") or item.get("id") or "")
        if not claim_id:
            continue
        row = connection.execute(
            "SELECT semantic.action,semantic.object,claim.subject_entity_id,claim.status "
            "FROM claim_relation_semantics semantic JOIN claims claim ON claim.id=semantic.claim_id "
            "WHERE semantic.claim_id=?",
            (claim_id,),
        ).fetchone()
        if row is None:
            continue
        metrics["available"] = int(metrics["available"]) + 1
        assert row["status"] == "active", "packet relation annotation requires an active claim"
        base_text = str(item.get("text") or "")
        rendered = render_memory_text(
            base_text,
            role=row["subject_entity_id"],
            action=row["action"],
            object_=row["object"],
        )
        current_cost = int(item.get("token_count") or estimate_tokens(str(item.get("rendered_text") or base_text)))
        next_cost = estimate_tokens(rendered)
        overhead = max(0, next_cost - current_cost)
        if used + overhead > token_budget:
            metrics["omitted_for_budget"] = int(metrics["omitted_for_budget"]) + 1
            continue
        item.update(
            {
                "role": str(row["subject_entity_id"]),
                "action": str(row["action"]),
                "object": str(row["object"]),
                "rendered_text": rendered,
                "token_count": next_cost,
            }
        )
        used += overhead
        metrics["rendered"] = int(metrics["rendered"]) + 1
        metrics["token_overhead"] = int(metrics["token_overhead"]) + overhead
    final_ids = [str(item.get("claim_id") or item.get("id") or "") for item in overlaid]
    metrics["claim_ids_preserved"] = final_ids == original_ids
    if not metrics["claim_ids_preserved"]:
        raise AssertionError("packet relation overlay changed claim IDs or order")
    return overlaid, metrics
