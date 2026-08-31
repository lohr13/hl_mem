"""Worker job dispatch and maintenance-job handlers."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable

from hl_mem import components
from hl_mem.domain.consolidation_scope import ConsolidationScope
from hl_mem.workers.automation import SEMANTIC_JOB_TYPES, semantic_job_enabled
from hl_mem.workers.decay import decay_claims
from hl_mem.workers.deduplicate import deduplicate_claims
from hl_mem.workers.discover_relations import discover_relations
from hl_mem.workers.induce_policies import induce_policies
from hl_mem.workers.rebuild_usefulness import rebuild_usefulness
from hl_mem.workers.scheduling import lease_deadline, utc_now
from hl_mem.workers.ttl import expire_claims

if TYPE_CHECKING:
    from hl_mem.workers.worker import Worker


def _handle_extract(worker: Worker, job: dict[str, Any]) -> dict[str, Any]:
    payload = job.get("payload") or json.loads(job["payload_json"] or "{}")
    return worker._extract(payload, job["id"], _extraction_progress_callback(worker, job))


def _extraction_progress_callback(
    worker: Worker,
    job: dict[str, Any],
) -> Callable[[str, int, int, dict[str, Any]], None]:
    def update(stage: str, processed: int, total: int, detail: dict[str, Any]) -> None:
        job_ids = list(job.get("leased_job_ids") or [job["id"]])
        updated = sum(
            worker.jobs.update_progress(
                job_id,
                job["lease_token"],
                stage=stage,
                processed=processed,
                total=total,
                detail=detail,
                heartbeat_at=utc_now(),
            )
            for job_id in job_ids
        )
        if updated != len(job_ids):
            raise RuntimeError("job lease ownership lost while reporting extraction writes")

    return update


def _handle_expire(worker: Worker, job: dict[str, Any]) -> dict[str, Any]:
    return expire_claims(
        worker.connection,
        feedback_lifecycle_mode=worker.settings.feedback_lifecycle_mode,
        slot_short_ttl_seconds=worker.settings.slot_short_ttl_seconds,
    )


def _handle_decay(worker: Worker, job: dict[str, Any]) -> dict[str, Any]:
    return decay_claims(
        worker.connection,
        temporal_decay_days=worker.settings.decay_temporal_days,
        temporal_archive_days=worker.settings.archive_temporal_days,
        permanent_decay_days=worker.settings.decay_permanent_days,
        permanent_archive_days=worker.settings.archive_permanent_days,
        access_bonus_every=worker.settings.access_bonus_every,
        access_bonus_days=worker.settings.access_bonus_days,
        access_bonus_cap_days=worker.settings.access_bonus_cap_days,
        rollout_grace_days=worker.settings.decay_rollout_grace_days,
        min_confidence=worker.settings.decay_min_confidence,
        feedback_lifecycle_mode=worker.settings.feedback_lifecycle_mode,
        feedback_bonus_cap_days=worker.settings.feedback_bonus_cap_days,
        decay_model=worker.settings.decay_model,
        temporal_half_life_days=worker.settings.decay_temporal_half_life_days,
        permanent_half_life_days=worker.settings.decay_permanent_half_life_days,
        identity_half_life_days=worker.settings.decay_identity_half_life_days,
        halflife_archive_threshold=worker.settings.decay_halflife_archive_threshold,
        halflife_archive_grace_days=worker.settings.decay_halflife_archive_grace_days,
    )


def _handle_rebuild_usefulness(worker: Worker, job: dict[str, Any]) -> dict[str, Any]:
    return rebuild_usefulness(worker.connection, worker.settings)


def _handle_consolidate(worker: Worker, job: dict[str, Any]) -> dict[str, Any]:
    consolidator = worker.consolidator or worker._make_consolidator()
    payload = json.loads(job["payload_json"] or "{}")
    scope = ConsolidationScope(
        namespace=payload.get("namespace", "default"),
        slot_filter=payload.get("slot_filter"),
        tag_filter=payload.get("tag_filter"),
        max_pairs=int(payload.get("max_pairs", payload.get("limit", worker.settings.consolidate_batch_size))),
        similarity_threshold=float(payload.get("similarity_threshold", 0.72)),
        similarity_ceiling=float(payload.get("similarity_ceiling", 0.95)),
    )
    return consolidator.run_batch(
        int(payload.get("limit", worker.settings.consolidate_batch_size)),
        payload.get("namespace", "default"),
        payload.get("watermark"),
        bool(payload.get("dry_run", False)),
        _job_progress_callback(worker, job),
        scope,
    )


def _handle_reconcile_plan_result(worker: Worker, job: dict[str, Any]) -> dict[str, Any]:
    from hl_mem.application.plan_fulfillment import PlanFulfillmentService

    payload = job.get("payload") or json.loads(job["payload_json"] or "{}")
    return PlanFulfillmentService(worker.connection, mode=worker.settings.plan_fulfillment_mode).reconcile(
        str(payload["result_claim_id"]), now=utc_now()
    )


def _handle_induce_policies(worker: Worker, job: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(job.get("payload_json") or "{}")
    return induce_policies(
        worker.connection,
        utc_now(),
        worker.settings.policy_induction_lookback_days,
        worker.settings.policy_induction_min_episodes,
        namespace=payload.get("namespace"),
    )


def _handle_deduplicate(worker: Worker, job: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(job["payload_json"] or "{}")
    return deduplicate_claims(
        worker.connection,
        components.make_llm_client(
            worker.settings,
            worker.connection,
            operation="dedup",
            runtime=worker._get_provider_runtime(),
        ),
        worker.embedder,
        namespace=str(payload.get("namespace", "default")),
        threshold=float(payload.get("threshold", worker.settings.dedup_threshold)),
        audit_only=bool(payload.get("audit_only", worker.settings.dedup_audit_only)),
        auto_merge_min_confidence=float(
            payload.get("auto_merge_min_confidence", worker.settings.dedup_auto_merge_min_confidence)
        ),
        limit=int(payload.get("limit", worker.settings.dedup_scan_limit)),
        progress_callback=_job_progress_callback(worker, job),
    )


def _handle_discover_relations(worker: Worker, job: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(job["payload_json"] or "{}")
    discoverer = worker.relation_discoverer or components.make_relation_discoverer(
        worker.settings,
        worker.connection,
        runtime=worker._get_provider_runtime(),
    )
    if discoverer is None:
        return {"candidates": 0, "proposals": 0, "rejected": 0}
    return discover_relations(
        worker.connection,
        discoverer,
        str(payload["claim_id"]),
        mode=worker.settings.relation_discovery_mode,
        pool_limit=worker.settings.relation_discovery_pool_limit,
        max_proposals=worker.settings.relation_discovery_max_proposals,
    )


def _job_progress_callback(worker: Worker, job: dict[str, Any]) -> Callable[[str, int, int], None]:
    def update(stage: str, processed: int, total: int) -> None:
        heartbeat_at = utc_now()
        job_ids = list(job.get("leased_job_ids") or [job["id"]])
        renewed = worker.jobs.renew_lease(
            job_ids,
            job["lease_token"],
            leased_until=lease_deadline(worker.settings),
            heartbeat_at=heartbeat_at,
        )
        updated = worker.jobs.update_progress(
            job["id"],
            job["lease_token"],
            stage=stage,
            processed=processed,
            total=total,
            heartbeat_at=heartbeat_at,
        )
        if renewed != len(job_ids) or not updated:
            raise RuntimeError("job lease ownership lost while reporting progress")

    return update


def _handle_reclassify(worker: Worker, job: dict[str, Any]) -> dict[str, Any]:
    from hl_mem.workers.reclassify import reclassify_claims

    return reclassify_claims(
        worker.connection,
        components.make_llm_client(
            worker.settings,
            worker.connection,
            operation="other",
            runtime=worker._get_provider_runtime(),
        ),
        policy=worker.settings.retention_policy(),
    )


def _handle_purge_retention(worker: Worker, job: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(job.get("payload_json") or "{}")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=worker.settings.retention_days)).isoformat()
    return {
        "purged": purge_retained_events_for_namespaces(
            worker.connection,
            cutoff,
            namespace=payload.get("namespace"),
        )
    }


def purge_retained_events_for_namespaces(
    connection: Any,
    cutoff: str,
    namespace: str | None = None,
) -> int:
    from hl_mem.security.retention import purge_retained_events

    namespaces = (
        [namespace]
        if namespace is not None
        else [
            str(row[0])
            for row in connection.execute("SELECT DISTINCT tenant_id FROM events ORDER BY tenant_id").fetchall()
        ]
    )
    return sum(purge_retained_events(connection, event_namespace, cutoff) for event_namespace in namespaces)


def _handle_retry_failed(worker: Worker, job: dict[str, Any]) -> dict[str, Any]:
    retried = worker.jobs.retry_failed()
    worker.connection.commit()
    return {"retried": retried}


JOB_HANDLERS: dict[str, Callable[[Worker, dict[str, Any]], dict[str, Any]]] = {
    "extract_event": _handle_extract,
    "expire_ttl": _handle_expire,
    "decay_access": _handle_decay,
    "rebuild_usefulness": _handle_rebuild_usefulness,
    "consolidate_conflicts": _handle_consolidate,
    "reconcile_plan_result": _handle_reconcile_plan_result,
    "deduplicate_claims": _handle_deduplicate,
    "discover_relations": _handle_discover_relations,
    "induce_policies": _handle_induce_policies,
    "reclassify_claims": _handle_reclassify,
    "purge_retention": _handle_purge_retention,
    "retry_failed": _handle_retry_failed,
}


def dispatch_job(worker: Worker, job: dict[str, Any]) -> dict[str, Any]:
    job_type = str(job["job_type"])
    if job_type in SEMANTIC_JOB_TYPES and not semantic_job_enabled(worker.settings, job_type):
        return {
            "status": "disabled",
            "reason": "disabled_by_configuration",
            "job_type": job_type,
        }
    handler = JOB_HANDLERS.get(job_type)
    if handler is None:
        raise ValueError(f"unknown job type: {job_type}")
    return handler(worker, job)
