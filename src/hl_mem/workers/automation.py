"""Explicit policy for background jobs that can call models or publish derived state."""

from __future__ import annotations

from typing import Literal

from hl_mem.settings import Settings

SemanticJobType = Literal[
    "consolidate_conflicts",
    "deduplicate_claims",
    "discover_relations",
    "induce_policies",
    "reclassify_claims",
]

SEMANTIC_JOB_TYPES: frozenset[str] = frozenset(
    {
        "consolidate_conflicts",
        "deduplicate_claims",
        "discover_relations",
        "induce_policies",
        "reclassify_claims",
    }
)


def semantic_job_enabled(settings: Settings, job_type: str) -> bool:
    """Return whether one explicitly governed semantic job may run."""
    if job_type == "consolidate_conflicts":
        return settings.semantic_conflict_consolidation_enabled
    if job_type == "deduplicate_claims":
        return settings.dedup_llm_enabled
    if job_type == "discover_relations":
        return settings.relation_discovery_mode == "audit"
    if job_type == "induce_policies":
        return settings.policy_induction_enabled
    if job_type == "reclassify_claims":
        return settings.reclassify_enabled
    raise ValueError(f"unknown semantic job type: {job_type}")
