"""Deterministic projection of the effective extraction route into memory."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass

from hl_mem.application.ingest import IngestService, _now, new_id
from hl_mem.domain.claims.attributes import SLOT_REGISTRY
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.settings import Settings
from hl_mem.storage.entities import EntityRepository
from hl_mem.storage.events import EventRepository

_PRODUCER_CONTRACT = "hl_mem.runtime-config-report-v1"


@dataclass(frozen=True)
class RuntimeConfigReport:
    claim_id: str | None
    fingerprint: str
    stored: bool
    reason: str


def _route_fingerprint(settings: Settings) -> str:
    route = {
        "model": settings.llm_model,
        "provider": settings.llm_provider,
        "producer_contract": _PRODUCER_CONTRACT,
    }
    payload = json.dumps(route, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _active_projection(db: sqlite3.Connection, namespace: str, fingerprint: str) -> str | None:
    row = db.execute(
        "SELECT id FROM claims WHERE namespace_key=? AND status IN ('active','candidate','disputed') "
        "AND subject_canonical_entity_id='project:hl_mem' AND canonical_slot='choice.model' "
        "AND json_extract(qualifiers_json,'$.task')='extraction' "
        "AND json_extract(qualifiers_json,'$.runtime_config')=1 "
        "AND json_extract(qualifiers_json,'$.config_fingerprint')=? "
        "ORDER BY recorded_from DESC,id DESC LIMIT 1",
        (namespace, fingerprint),
    ).fetchone()
    return str(row[0]) if row is not None else None


def report_extraction_runtime(
    db: sqlite3.Connection,
    settings: Settings,
    *,
    namespace: str = "default",
) -> RuntimeConfigReport:
    """Persist one idempotent, source-independent report of the active extraction route."""
    if settings.extractor_mode == "fake":
        return RuntimeConfigReport(None, "", False, "fake_profile")
    fingerprint = _route_fingerprint(settings)
    EntityRepository(db).seed_builtins(namespace, now=_now())
    existing_id = _active_projection(db, namespace, fingerprint)
    if existing_id is not None:
        return RuntimeConfigReport(existing_id, fingerprint, False, "unchanged")

    observed_at = _now()
    event = {
        "id": new_id(),
        "tenant_id": namespace,
        "event_type": "runtime_config_report",
        "actor_type": "tool",
        "content": {
            "schema_version": "runtime_config_report_v1",
            "producer_contract": _PRODUCER_CONTRACT,
            "capability": "extraction",
            "provider": settings.llm_provider,
            "model": settings.llm_model,
            "config_fingerprint": fingerprint,
            "observed_at": observed_at,
        },
        "occurred_at": observed_at,
        "recorded_at": observed_at,
        "origin_class": "agent",
        "session_kind": "unknown",
    }
    EventRepository(db).insert_event(event)
    result = IngestService.store_extracted(
        db,
        ExtractedClaim(
            predicate=SLOT_REGISTRY["choice.model"].predicate,
            value=f"HL-Mem LLM 提取任务使用 {settings.llm_provider}/{settings.llm_model}",
            subject="HL-Mem",
            qualifiers={
                "task": "extraction",
                "provider": settings.llm_provider,
                "state_change": True,
                "runtime_config": True,
                "config_fingerprint": fingerprint,
            },
            canonical_attribute="choice.model",
            canonical_slot="choice.model",
            scope="permanent",
            importance=0.9,
            assertion_kind="observation",
        ),
        event,
        observed_at,
        FakeEmbedder(settings.embedding_dim),
        authority="high",
    )
    if result.claim_id is None:
        raise RuntimeError(f"runtime extraction route was not persisted: {result.reason}")
    return RuntimeConfigReport(result.claim_id, fingerprint, True, "stored")
