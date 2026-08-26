import json
import sqlite3
from datetime import datetime

import hl_mem
from hl_mem.application.ingest import IngestService, _now, new_id
from hl_mem.domain.claims.attributes import SLOT_REGISTRY
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.settings import Settings
from hl_mem.state_latest_wins import CurrentnessProof, parse_version_atom
from hl_mem.storage.database import Database
from hl_mem.storage.entities import EntityRepository
from hl_mem.storage.events import EventRepository


def report_version(db: sqlite3.Connection, *, namespace: str, subject: str) -> dict[str, object]:
    observed_at = _now()
    runtime_version = hl_mem.__version__
    parsed_time = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    if not namespace.strip() or parsed_time.tzinfo is None or parse_version_atom(runtime_version) is None:
        raise ValueError("report-version namespace, runtime version, or timestamp is invalid")
    contract = ("status_report_v1", "hl_mem.report-version-v1", "hl_mem")
    entities = EntityRepository(db)
    entities.seed_builtins(namespace, now=observed_at)
    if (owner := entities.resolve_alias(subject, namespace_key=namespace, role="subject")) is None:
        raise ValueError("report-version subject has no unique active typed alias")
    owner_id = owner.canonical_entity_id
    event = {
        "id": new_id(),
        "tenant_id": namespace,
        "event_type": "status_report",
        "actor_type": "tool",
        "content": {
            "schema_version": contract[0],
            "producer_contract": contract[1],
            "package": contract[2],
            "runtime_version": runtime_version,
            "namespace": namespace,
            "subject_proof": {"canonical_entity_id": owner_id, "alias_version": owner.alias_version},
            "observed_at": observed_at,
        },
        "occurred_at": observed_at,
        "recorded_at": observed_at,
    }
    EventRepository(db).insert_event(event)
    proof = CurrentnessProof(*contract, runtime_version, namespace, owner_id, owner.alias_version, observed_at, True)
    result = IngestService.store_extracted(
        db,
        ExtractedClaim(
            SLOT_REGISTRY["config.version"].predicate,
            runtime_version,
            subject=subject,
            canonical_attribute="config.version",
            canonical_slot="config.version",
            assertion_kind="observation",
        ),
        event,
        observed_at,
        FakeEmbedder(int(getattr(getattr(db, "hl_mem_settings", None), "embedding_dim", 2048))),
        authority="high",
        currentness_proof=proof,
        _trusted_projector_slot="config.version",
    )
    return {
        "event_id": event["id"],
        "owner": owner_id,
        "reported_version": runtime_version,
        "producer_contract": contract[1],
        "queued": False,
        "stored": result.status == "stored",
    }


def report_version_cli(settings: Settings, namespace: str, subject: str) -> str:
    database = Database(settings=settings)
    try:
        result = report_version(database.open(), namespace=namespace, subject=subject)
    finally:
        database.close()
    return json.dumps(result, ensure_ascii=False, sort_keys=True)
