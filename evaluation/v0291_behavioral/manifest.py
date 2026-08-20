"""Load and expand the frozen v0.29.1 freshness behavioral manifest."""

from __future__ import annotations

import copy
import json
import re
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA_VERSION = "v0291-freshness-behavioral-manifest-v1"
EVAL_MANIFEST_VERSION = "v0291-behavioral-eval-v1"
MODEL_SNAPSHOT = "qwen3.7-plus-2026-05-26"
EXPECTED_COUNTS = {
    "incident": 20,
    "stale_positive": 20,
    "stable_negative": 20,
    "correction_backed": 10,
    "boundary": 10,
}
_OPAQUE_ID = re.compile(r"^[0-9a-f]{32}$")
_APPLICABILITY = frozenset({"stale-positive", "stable-negative", "boundary"})
_DIMENSIONS = frozenset(
    {
        "obsolete_acceptance",
        "verification_action",
        "stable_fact_disposition",
        "final_attribution",
        "unsupported_new_configuration",
    }
)


def load_behavioral_manifest(path: Path) -> dict[str, Any]:
    """Read and validate the compact, human-authored behavioral manifest."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("behavioral manifest must be a JSON object")
    if payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("unsupported behavioral manifest schema")
    if payload.get("eval_manifest_version") != EVAL_MANIFEST_VERSION:
        raise ValueError("unexpected eval manifest version")
    if payload.get("model_snapshot") != MODEL_SNAPSHOT:
        raise ValueError("behavioral manifest must use the frozen model snapshot")
    rendering_now = datetime.fromisoformat(str(payload.get("rendering_now", "")))
    if rendering_now.tzinfo is None:
        raise ValueError("rendering_now must include a timezone")
    tool_contract = payload.get("tool_contract")
    if not isinstance(tool_contract, dict) or not isinstance(tool_contract.get("tools"), list):
        raise ValueError("tool_contract.tools must be a list")
    samples = expand_behavioral_samples(payload)
    counts = Counter(sample["cohort"] for sample in samples)
    if counts != EXPECTED_COUNTS:
        raise ValueError(f"behavioral cohort counts differ from frozen counts: {dict(counts)}")
    return payload


def expand_behavioral_samples(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Expand prompt variants without synthesizing any label or expected answer."""

    raw_cohorts = manifest.get("cohorts")
    if not isinstance(raw_cohorts, list):
        raise ValueError("behavioral manifest cohorts must be a list")
    samples: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for cohort in raw_cohorts:
        if not isinstance(cohort, dict):
            raise TypeError("behavioral cohort must be an object")
        name = cohort.get("name")
        defaults = cohort.get("defaults")
        scenarios = cohort.get("scenarios")
        if name not in EXPECTED_COUNTS or not isinstance(defaults, dict) or not isinstance(scenarios, list):
            raise ValueError("behavioral cohort has an invalid name, defaults, or scenarios")
        for scenario in scenarios:
            if not isinstance(scenario, dict):
                raise TypeError("behavioral scenario must be an object")
            sample_ids = scenario.get("sample_ids")
            prompts = scenario.get("user_prompts")
            if not isinstance(sample_ids, list) or not isinstance(prompts, list) or len(sample_ids) != len(prompts):
                raise ValueError("sample_ids and user_prompts must be equally sized lists")
            if not sample_ids:
                raise ValueError("behavioral scenario must contain at least one prompt")
            common = copy.deepcopy(defaults)
            common.update(
                {
                    key: copy.deepcopy(value)
                    for key, value in scenario.items()
                    if key not in {"sample_ids", "user_prompts"}
                }
            )
            for sample_id, prompt in zip(sample_ids, prompts, strict=True):
                if not isinstance(sample_id, str) or not _OPAQUE_ID.fullmatch(sample_id) or sample_id in seen_ids:
                    raise ValueError("opaque sample IDs must be unique 32-character lowercase hex strings")
                if not isinstance(prompt, str) or not prompt.strip():
                    raise ValueError("user prompts must be non-empty strings")
                sample = copy.deepcopy(common)
                sample.update(
                    {
                        "opaque_sample_id": sample_id,
                        "cohort": name,
                        "user_prompt": prompt,
                    }
                )
                _validate_expanded_sample(sample)
                samples.append(sample)
                seen_ids.add(sample_id)
    return samples


def _validate_expanded_sample(sample: Mapping[str, Any]) -> None:
    required = (
        "scenario_family_id",
        "gold_source",
        "current_truth",
        "stale_or_stable_reference",
        "allowed_verification_actions",
        "harmful_or_write_actions",
        "deterministic_tool_results",
        "reference",
        "intent",
        "delivery_purpose",
        "expected_applicability",
        "applicable_dimensions",
    )
    missing = [key for key in required if key not in sample]
    if missing:
        raise ValueError(f"behavioral sample is missing fields: {missing}")
    if sample["expected_applicability"] not in _APPLICABILITY:
        raise ValueError("invalid expected_applicability")
    dimensions = sample["applicable_dimensions"]
    if not isinstance(dimensions, list) or not set(dimensions) <= _DIMENSIONS:
        raise ValueError("invalid applicable_dimensions")
    for field in ("allowed_verification_actions", "harmful_or_write_actions"):
        if not isinstance(sample[field], list):
            raise TypeError(f"{field} must be a list")
    if not isinstance(sample["deterministic_tool_results"], dict):
        raise TypeError("deterministic_tool_results must be an object")
    reference = sample["reference"]
    if not isinstance(reference, dict) or not isinstance(reference.get("text"), str):
        raise ValueError("reference must contain text")
