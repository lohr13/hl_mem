"""Frozen v0.30 experiment manifest contracts and privacy helpers."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "v030-corpus-1"
EXPERIMENTS = frozenset({"E1", "E2", "E3", "E4", "E5", "E6"})
_CATEGORY_MINIMUMS: dict[str, dict[str, int]] = {
    "E3": {
        "correction": 50,
        "guardrail": 50,
        "high_cost": 40,
        "persistent_instruction": 40,
        "bait_negative": 30,
        "ordinary": 30,
    },
    "E4": {"unique_alias": 80, "ambiguous": 60, "no_entity": 50, "multi_entity": 50},
    "E5": {"complete": 35, "cancel": 25, "replace": 25, "partial": 30, "ambiguous_negative": 25},
}
_E5_RISK_TAGS = frozenset({"innovation_variants", "gold_10500", "negation", "cross_account", "unordered_partial"})
_REDACTIONS = (
    (re.compile(r"(?i)\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b"), "<EMAIL>"),
    (re.compile(r"(?i)\bhttps?://[^\s]+"), "<URL>"),
    (re.compile(r"(?i)\b[a-z]:\\(?:[^\\\s]+\\)*[^\\\s]*"), "<PATH>"),
    (re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])"), "<IP>"),
    (re.compile(r"(?i)\b[0-9a-f]{24,}\b"), "<OPAQUE_ID>"),
)


def _canonical_payload(value: Mapping[str, Any]) -> bytes:
    payload = {key: item for key, item in value.items() if key != "manifest_sha256"}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def manifest_sha256(manifest: Mapping[str, Any]) -> str:
    """Hash semantic manifest content independently of JSON formatting."""

    return hashlib.sha256(_canonical_payload(manifest)).hexdigest()


def redact_text(text: str) -> str:
    """Apply deterministic direct-identifier redaction to evaluation text."""

    redacted = unicodedata.normalize("NFC", text)
    for pattern, replacement in _REDACTIONS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _validate_common(manifest: Mapping[str, Any]) -> tuple[str, list[Mapping[str, Any]]]:
    experiment = str(manifest.get("experiment") or "")
    if manifest.get("schema_version") != SCHEMA_VERSION or experiment not in EXPERIMENTS:
        raise ValueError("v030 manifest schema or experiment is invalid")
    raw_cases = manifest.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError(f"{experiment} cases must be a non-empty list")
    cases: list[Mapping[str, Any]] = []
    case_ids: list[str] = []
    for index, raw in enumerate(raw_cases):
        if not isinstance(raw, Mapping):
            raise ValueError(f"{experiment} case {index} must be an object")
        case_id = str(raw.get("case_id") or "")
        if not case_id or not all(isinstance(raw.get(key), Mapping) for key in ("input", "gold")):
            raise ValueError(f"{experiment} case {index} schema is invalid")
        if not str(raw.get("source") or "") or not str(raw.get("category") or ""):
            raise ValueError(f"{experiment} case {index} provenance is invalid")
        cases.append(raw)
        case_ids.append(case_id)
    if len(case_ids) != len(set(case_ids)):
        raise ValueError(f"{experiment} requires unique case_id values")
    snapshots = manifest.get("source_snapshots")
    if not isinstance(snapshots, list) or not snapshots:
        raise ValueError(f"{experiment} requires source snapshots")
    for snapshot in snapshots:
        if not isinstance(snapshot, Mapping) or not re.fullmatch(r"[0-9a-f]{64}", str(snapshot.get("sha256") or "")):
            raise ValueError(f"{experiment} source snapshot hash is invalid")
        if not isinstance(snapshot.get("reconstructable"), bool):
            raise ValueError(f"{experiment} source reconstructability is invalid")
    expected_hash = manifest.get("manifest_sha256")
    if expected_hash is not None and expected_hash != manifest_sha256(manifest):
        raise ValueError(f"{experiment} manifest SHA256 mismatch")
    return experiment, cases


def _require_category_minimums(experiment: str, counts: Counter[str]) -> None:
    for category, minimum in _CATEGORY_MINIMUMS[experiment].items():
        if counts[category] < minimum:
            raise ValueError(f"{experiment} requires at least {minimum} {category} cases")


def validate_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one frozen manifest and return deterministic gate counts."""

    experiment, cases = _validate_common(manifest)
    sources = Counter(str(case["source"]) for case in cases)
    categories = Counter(str(case["category"]) for case in cases)
    decisions = Counter(str(case["gold"].get("decision") or "") for case in cases)
    risk_tags = sorted({str(tag) for case in cases for tag in case.get("risk_tags", [])})
    if experiment == "E1":
        if len(cases) != 70 or sources != Counter({"local": 59, "volcano": 11}):
            raise ValueError("E1 requires exactly 70 cases split local=59 and volcano=11")
        allowed = {"keep_left", "keep_right", "coexist", "select_candidate", "reject", "other"}
        if not decisions or not set(decisions) <= allowed:
            raise ValueError("E1 gold decision is invalid")
    elif experiment == "E2":
        negative = categories["distinct"] + categories["uncertain"]
        if categories["equivalent"] < 203 or negative < 203 or len(cases) < 406:
            raise ValueError("E2 requires 203 equivalent and 203 distinct/uncertain cases")
    elif experiment in _CATEGORY_MINIMUMS:
        _require_category_minimums(experiment, categories)
        if experiment == "E5" and not _E5_RISK_TAGS <= set(risk_tags):
            raise ValueError("E5 required risk scenarios are missing")
    elif experiment == "E6":
        instruments = {str(case.get("instrument_id") or "") for case in cases}
        instruments.discard("")
        if len(cases) < 120 or len(instruments) < 20:
            raise ValueError("E6 requires 120 price claims across 20 instruments")
    return {
        "experiment": experiment,
        "case_count": len(cases),
        "source_counts": dict(sorted(sources.items())),
        "category_counts": dict(sorted(categories.items())),
        "decision_counts": dict(sorted(decisions.items())),
        "risk_tags": risk_tags,
    }


def build_manifest(
    experiment: str,
    cases: Sequence[Mapping[str, Any]],
    *,
    source_snapshots: Sequence[Mapping[str, Any]],
    source_audit: Mapping[str, Any],
) -> dict[str, Any]:
    """Canonicalize, validate, and seal a manifest without wall-clock input."""

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "design_baseline": "2026-08-25",
        "experiment": experiment,
        "source_snapshots": sorted(
            (copy.deepcopy(dict(item)) for item in source_snapshots), key=lambda x: x["source_id"]
        ),
        "source_audit": copy.deepcopy(dict(source_audit)),
        "cases": sorted((copy.deepcopy(dict(case)) for case in cases), key=lambda x: x["case_id"]),
    }
    validate_manifest(manifest)
    manifest["manifest_sha256"] = manifest_sha256(manifest)
    validate_manifest(manifest)
    return manifest


def write_manifest(path: str | Path, manifest: Mapping[str, Any]) -> None:
    """Write a validated manifest in stable UTF-8 JSON form."""

    validate_manifest(manifest)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_manifest(path: str | Path) -> dict[str, Any]:
    """Load and authenticate a frozen manifest."""

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("v030 manifest root must be an object")
    validate_manifest(raw)
    return raw
