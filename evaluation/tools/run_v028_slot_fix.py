"""Score the v0.28 canonical-slot narrow fix on a frozen v0.27 input snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from hl_mem.domain.claims.attributes import SLOT_REGISTRY, validate_slot_instance
from hl_mem.ingest.llm_extractor import PROMPT_HASH, LLMExtractor

DEFAULT_SNAPSHOT = Path("var/eval/v028_slot_fix_inputs_v027.json")
DEFAULT_REPORT = Path("var/eval/v028_slot_fix_report.json")
DEFAULT_MARKDOWN = Path("var/eval/v028_slot_fix_report.md")


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_snapshot(path: Path) -> dict[str, Any]:
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    if snapshot.get("schema_version") != 1:
        raise ValueError("slot input snapshot schema_version must be 1")
    source_contract = snapshot.get("source_contract")
    if not isinstance(source_contract, Mapping) or source_contract.get("contract_id") != "compact-7field-v1":
        raise ValueError("slot input snapshot must use the frozen compact-7field-v1 contract")
    if source_contract.get("new_prompt_cache_used") is not False:
        raise ValueError("slot input snapshot must not use the new prompt cache")
    cases = snapshot.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("slot input snapshot cases must be a non-empty list")
    actual_hash = _canonical_hash(cases)
    if snapshot.get("cases_sha256") != actual_hash:
        raise ValueError("slot input snapshot cases_sha256 mismatch")
    return snapshot


def _required_qualifiers(slot: str | None, qualifiers: Mapping[str, Any]) -> dict[str, Any]:
    definition = SLOT_REGISTRY.get(str(slot or ""))
    if definition is None:
        return {}
    return {key: qualifiers[key] for key in definition.required_qualifiers if key in qualifiers}


def _new_instance(case: Mapping[str, Any]) -> dict[str, Any]:
    attribute = str(case["canonical_attribute"])
    qualifiers = LLMExtractor._infer_compact_qualifiers(
        attribute,
        str(case["subject"]),
        str(case["value"]),
        str(case["evidence"]),
    )
    slot = validate_slot_instance(attribute, qualifiers)
    return {
        "canonical_slot": slot,
        "required_qualifiers": _required_qualifiers(slot, qualifiers),
    }


def score_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    cases = snapshot["cases"]
    old_mismatches = 0
    new_mismatches = 0
    paired = Counter({"fixed": 0, "regressed": 0, "unchanged_wrong": 0, "unchanged_correct": 0})
    slices: Counter[str] = Counter()
    details: list[dict[str, Any]] = []
    for case in cases:
        expected = case["expected"]
        old = case["old"]
        new = _new_instance(case)
        old_wrong = old != expected
        new_wrong = new != expected
        old_mismatches += int(old_wrong)
        new_mismatches += int(new_wrong)
        slices[str(case["slice"])] += 1
        if old_wrong and not new_wrong:
            outcome = "fixed"
        elif not old_wrong and new_wrong:
            outcome = "regressed"
        elif old_wrong:
            outcome = "unchanged_wrong"
        else:
            outcome = "unchanged_correct"
        paired[outcome] += 1
        details.append(
            {
                "case_id": case["case_id"],
                "slice": case["slice"],
                "old": old,
                "new": new,
                "expected": expected,
                "outcome": outcome,
            }
        )
    count = len(cases)
    return {
        "schema_version": 1,
        "snapshot_id": snapshot["snapshot_id"],
        "cases_sha256": snapshot["cases_sha256"],
        "product_prompt_hash": PROMPT_HASH,
        "case_count": count,
        "slices": dict(sorted(slices.items())),
        "old": {"mismatches": old_mismatches, "mismatch_rate": old_mismatches / count},
        "new": {"mismatches": new_mismatches, "mismatch_rate": new_mismatches / count},
        "paired": dict(paired),
        "cases": details,
    }


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _write_markdown(path: Path, report: Mapping[str, Any]) -> None:
    old = report["old"]
    new = report["new"]
    paired = report["paired"]
    lines = [
        "# v0.28 canonical_slot narrow-fix paired evaluation",
        "",
        f"- snapshot: `{report['snapshot_id']}`",
        f"- cases SHA-256: `{report['cases_sha256']}`",
        f"- cases: {report['case_count']}",
        "",
        "| arm | mismatches | rate |",
        "|---|---:|---:|",
        f"| v0.27 old | {old['mismatches']} | {old['mismatch_rate']:.2%} |",
        f"| v0.28 narrow fix | {new['mismatches']} | {new['mismatch_rate']:.2%} |",
        "",
        f"Paired: fixed={paired['fixed']}, regressed={paired['regressed']}, "
        f"unchanged_wrong={paired['unchanged_wrong']}, unchanged_correct={paired['unchanged_correct']}.",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--markdown-output", type=Path, default=DEFAULT_MARKDOWN)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = score_snapshot(load_snapshot(args.snapshot))
    report["created_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(args.output, report)
    _write_markdown(args.markdown_output, report)
    print(json.dumps({"report": str(args.output), "mismatches": report["new"]["mismatches"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
