"""通过统一 LLMClient 执行存量记忆重分类。"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

from hl_mem import components
from hl_mem.config_loader import load_settings
from hl_mem.domain.claims.attributes import validate_slot_instance
from hl_mem.domain.claims.conflicts import compute_conflict_key
from hl_mem.domain.claims.retention import TTLPolicy, compute_expiration
from hl_mem.errors import ActiveClaimInvariantError, ConfigurationError
from hl_mem.llm.client import LLMClient
from hl_mem.llm.types import (
    LLMMessage,
    LLMRequest,
    StructuredOutputMode,
    StructuredOutputSpec,
)
from hl_mem.observability.audit import current_audit
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database

CLASSIFY_PROMPT = """Classify each supplied memory without extracting or rewriting it.
Return JSON {"classifications":[{"id":...,"scope":"temporal|permanent","importance":0.0-1.0}]}.
Scope is independent from volatility: temporal is useful for a bounded real-world period;
permanent is a durable preference, identity, convention, configuration, or long-term memory.
Importance: 0.0-0.3 incidental, 0.4-0.6 useful, 0.7-0.9 important, 1.0 must remember.
Do not infer importance merely from emotional wording."""


def _chunks(values: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _text(claim: dict[str, Any]) -> str:
    value = claim.get("value")
    return f"{claim.get('subject_entity_id') or ''} {claim.get('predicate') or ''} {value or ''}".strip()


def _is_deterministic_version_probe(connection: Any, claim: dict[str, Any]) -> bool:
    """Keep only provenance-backed report-version projections outside LLM rewrites."""
    if not (
        claim.get("canonical_slot") == "config.version"
        and claim.get("canonical_attribute") == "config.version"
        and claim.get("assertion_kind") == "observation"
        and claim.get("source_authority") == "high"
        and isinstance(claim.get("subject_canonical_entity_id"), str)
    ):
        return False
    rows = connection.execute(
        "SELECT event.tenant_id,event.event_type,event.actor_type,event.occurred_at,event.content_json "
        "FROM evidence_links AS link JOIN events AS event "
        "ON event.id=link.evidence_id AND link.evidence_type='event' "
        "WHERE link.derived_type='claim' AND link.derived_id=? "
        "AND link.relation IN ('derived_from','supports')",
        (claim["id"],),
    ).fetchall()
    for row in rows:
        try:
            content = json.loads(str(row["content_json"]))
        except (TypeError, ValueError):
            continue
        if not isinstance(content, dict):
            continue
        proof = content.get("subject_proof")
        if not isinstance(proof, dict):
            continue
        alias_version = proof.get("alias_version")
        if (
            row["tenant_id"] == claim.get("namespace_key")
            and row["event_type"] == "status_report"
            and row["actor_type"] == "tool"
            and content.get("schema_version") == "status_report_v1"
            and content.get("producer_contract") == "hl_mem.report-version-v1"
            and content.get("package") == "hl_mem"
            and content.get("runtime_version") == claim.get("value")
            and content.get("namespace") == claim.get("namespace_key")
            and content.get("observed_at") == row["occurred_at"]
            and proof.get("canonical_entity_id") == claim.get("subject_canonical_entity_id")
            and isinstance(alias_version, int)
            and not isinstance(alias_version, bool)
            and alias_version >= 1
        ):
            return True
    return False


def classify_batch(llm_client: LLMClient, claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """通过统一 LLMClient 批量重分类记忆。"""
    response = llm_client.complete(
        LLMRequest(
            messages=[
                LLMMessage(role="system", content=CLASSIFY_PROMPT),
                LLMMessage(
                    role="user",
                    content=json.dumps(
                        [{"id": claim["id"], "text": _text(claim)} for claim in claims],
                        ensure_ascii=False,
                    ),
                ),
            ],
            structured_output=StructuredOutputSpec(
                name="memory_classifications",
                schema={
                    "type": "object",
                    "properties": {
                        "classifications": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "scope": {
                                        "type": "string",
                                        "enum": ["temporal", "permanent"],
                                    },
                                    "importance": {
                                        "type": "number",
                                        "minimum": 0,
                                        "maximum": 1,
                                    },
                                },
                                "required": ["id", "scope", "importance"],
                                "additionalProperties": False,
                            },
                        }
                    },
                    "required": ["classifications"],
                    "additionalProperties": False,
                },
                preferred_mode=StructuredOutputMode.JSON_SCHEMA,
            ),
        )
    )
    parsed = response.content if isinstance(response.content, dict) else json.loads(response.content)
    values = parsed.get("classifications", [])
    return values if isinstance(values, list) else []


def _classification_expiration(
    claim: dict[str, Any],
    scope: str,
    importance: float,
    policy: TTLPolicy,
) -> str | None:
    """按原始观察时间重算绝对过期时间，且不改写已过期 claim。"""
    if claim.get("status") == "expired":
        return claim.get("expires_at")
    observed_at = str(claim.get("observed_at") or "")
    recorded_from = str(claim.get("recorded_from") or "")
    if not observed_at and not recorded_from:
        return claim.get("expires_at")
    expires_at, _reason = compute_expiration(
        scope=scope,
        importance=importance,
        volatility=str(claim.get("volatility") or "stable"),
        canonical_slot=validate_slot_instance(claim.get("canonical_slot"), claim.get("qualifiers")),
        valid_to=claim.get("valid_to"),
        observed_at=observed_at,
        recorded_from=recorded_from,
        policy=policy,
        canonical_attribute=claim.get("canonical_attribute"),
    )
    return expires_at


def reclassify_claims(
    connection: Any,
    llm_client: LLMClient,
    batch_size: int = 8,
    temporal_ttl_days: int | None = None,
    policy: TTLPolicy | None = None,
) -> dict[str, int]:
    """重分类仍处于默认 scope/importance 的记忆。"""
    if not 5 <= batch_size <= 10:
        raise ValueError("batch_size must be between 5 and 10")
    effective_policy = policy or Settings().retention_policy()
    if temporal_ttl_days is not None:
        effective_policy = TTLPolicy(
            temporal_ttl_days_low=effective_policy.temporal_ttl_days_low,
            temporal_ttl_days_normal=temporal_ttl_days,
            temporal_ttl_days_high=effective_policy.temporal_ttl_days_high,
            importance_low_threshold=effective_policy.importance_low_threshold,
            importance_high_threshold=effective_policy.importance_high_threshold,
            importance_write_floor=effective_policy.importance_write_floor,
            slot_short_ttl_seconds=effective_policy.slot_short_ttl_seconds,
            short_ttl_slots=effective_policy.short_ttl_slots,
        )
    if effective_policy.temporal_ttl_days_normal < 1:
        raise ValueError("temporal_ttl_days must be positive")
    repository = ClaimRepository(connection)
    rows = repository.list_all()
    pending = [
        row
        for row in rows
        if row.get("status") != "expired"
        and row.get("scope", "permanent") == "permanent"
        and float(row.get("importance", 0.5)) == 0.5
        and not _is_deterministic_version_probe(connection, row)
    ]
    updated = 0
    guarded = 0
    for batch in _chunks(pending, batch_size):
        allowed_ids = {claim["id"] for claim in batch}
        for item in classify_batch(llm_client, batch):
            claim_id = item.get("id")
            if not isinstance(claim_id, str) or claim_id not in allowed_ids:
                continue
            scope = item.get("scope", "permanent")
            scope = scope if scope in {"temporal", "permanent"} else "permanent"
            try:
                importance = min(1.0, max(0.0, float(item.get("importance", 0.5))))
            except (TypeError, ValueError):
                importance = 0.5
            claim = next(candidate for candidate in batch if candidate["id"] == claim_id)
            canonical_slot = validate_slot_instance(claim.get("canonical_slot"), claim.get("qualifiers"))
            conflict_key = compute_conflict_key(
                str(claim.get("namespace_key") or "default"),
                str(claim.get("subject_entity_id") or ""),
                str(claim.get("predicate") or ""),
                canonical_slot,
                claim.get("qualifiers"),
            )
            expires_at = _classification_expiration(claim, scope, importance, effective_policy)
            try:
                changed = repository.update_classification(
                    claim_id,
                    scope,
                    importance,
                    canonical_slot,
                    expires_at,
                    conflict_key,
                )
            except ActiveClaimInvariantError:
                guarded += 1
                current_audit().emit(
                    "maintenance",
                    "reclassify_conflict_guard",
                    "blocked",
                    claim_id=claim_id,
                    detail={"canonical_slot": canonical_slot, "conflict_key": conflict_key},
                )
            else:
                updated += int(changed)
        connection.commit()
    return {"scanned": len(rows), "eligible": len(pending), "updated": updated, "guarded": guarded}


def main() -> None:
    """运行一次记忆重分类。"""
    parser = argparse.ArgumentParser(prog="python -m hl_mem.workers.reclassify")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--db")
    parser.add_argument("--batch-size", type=int, default=8)
    args = parser.parse_args()
    settings = load_settings(args.config, args.env_file)
    if args.db is not None:
        settings = replace(settings, database_path=args.db)
    database = Database(settings=settings)
    provider_runtime = None
    try:
        try:
            provider_runtime = components.create_provider_runtime(settings)
            llm_client = components.make_llm_client(settings, runtime=provider_runtime)
        except ConfigurationError as error:
            raise SystemExit("LLM_API_KEY is required") from error
        print(
            json.dumps(
                reclassify_claims(
                    database.open(),
                    llm_client,
                    args.batch_size,
                    policy=settings.retention_policy(),
                ),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    finally:
        if provider_runtime is not None:
            provider_runtime.close()
        database.close()


if __name__ == "__main__":
    main()
