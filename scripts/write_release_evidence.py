#!/usr/bin/env python
"""Validate release-gate outputs and write one auditable evidence manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Sequence

REQUIRED_EVIDENCE = frozenset(
    {
        "python-3.12",
        "python-3.13",
        "python-3.14",
        "migration",
        "backup-restore",
        "plugin-conflict",
        "streaming-limit",
        "zero-model-call",
        "public-recall",
        "pip-audit",
        "sbom",
        "wheel-install",
    }
)
PASSING_STATUSES = frozenset({"ok", "pass", "passed", "success", "succeeded"})
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assignment(value: str) -> tuple[str, Path]:
    name, separator, raw_path = value.partition("=")
    if not separator or not name.strip() or not raw_path.strip():
        raise ValueError(f"evidence must use NAME=PATH: {value!r}")
    return name.strip(), Path(raw_path).resolve()


def _junit_summary(path: Path) -> dict[str, int]:
    root = ET.parse(path).getroot()
    suites = (
        [root]
        if root.tag == "testsuite"
        else [suite for suite in root.findall(".//testsuite") if not suite.findall("testsuite")]
    )
    if not suites:
        raise ValueError(f"JUnit file has no test suites: {path}")
    summary = {
        field: sum(int(suite.attrib.get(field, 0)) for suite in suites)
        for field in ("tests", "failures", "errors", "skipped")
    }
    if summary["tests"] < 1:
        raise ValueError(f"JUnit file contains no tests: {path}")
    return summary


def validate_pip_audit(path: Path) -> dict[str, int]:
    """Validate both legacy-list and current-object pip-audit JSON."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    dependencies = (
        payload if isinstance(payload, list) else payload.get("dependencies") if isinstance(payload, dict) else None
    )
    if not isinstance(dependencies, list) or any(not isinstance(dependency, dict) for dependency in dependencies):
        raise ValueError(f"pip-audit JSON has invalid dependencies: {path}")
    vulnerabilities = [vulnerability for dependency in dependencies for vulnerability in dependency.get("vulns", [])]
    if vulnerabilities:
        raise ValueError(f"pip-audit reports {len(vulnerabilities)} vulnerabilities")
    return {"dependencies": len(dependencies), "vulnerabilities": 0}


def _json_status(name: str, path: Path) -> dict[str, Any]:
    if name == "pip-audit":
        return validate_pip_audit(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON evidence must be an object: {path}")
    if payload.get("ok") is False:
        raise ValueError(f"{name} reports ok=false")
    raw_status = payload.get("gate_status", payload.get("status"))
    if raw_status is not None and str(raw_status).casefold() not in PASSING_STATUSES:
        raise ValueError(f"{name} reports non-passing status {raw_status!r}")
    if name == "public-recall":
        if int(payload.get("case_count", 0)) < 1:
            raise ValueError("public-recall has no cases")
        if float(payload.get("http_success_rate", 0.0)) != 1.0:
            raise ValueError("public-recall HTTP success rate is not 1.0")
        if int(payload.get("total_forbidden_hits", -1)) != 0:
            raise ValueError("public-recall contains forbidden hits")
    return {"keys": sorted(payload)}


def _collect(
    junit_values: Sequence[str],
    json_values: Sequence[str],
    file_values: Sequence[str],
) -> dict[str, dict[str, Any]]:
    evidence: dict[str, dict[str, Any]] = {}
    for kind, values in (("junit", junit_values), ("json", json_values), ("file", file_values)):
        for value in values:
            name, path = _assignment(value)
            if name in evidence:
                raise ValueError(f"duplicate evidence name: {name}")
            if not path.is_file() or path.stat().st_size < 1:
                raise ValueError(f"evidence file is missing or empty: {path}")
            details: dict[str, Any]
            if kind == "junit":
                details = _junit_summary(path)
                if details["failures"] + details["errors"]:
                    raise ValueError(f"{name} JUnit evidence contains failures or errors")
            elif kind == "json":
                details = _json_status(name, path)
            else:
                details = {"bytes": path.stat().st_size}
            evidence[name] = {
                "status": "passed",
                "kind": kind,
                "artifact": path.name,
                "sha256": _sha256(path),
                "details": details,
            }
    missing = sorted(REQUIRED_EVIDENCE - set(evidence))
    unexpected = sorted(set(evidence) - REQUIRED_EVIDENCE)
    if missing:
        raise ValueError(f"missing required evidence: {', '.join(missing)}")
    if unexpected:
        raise ValueError(f"unexpected evidence: {', '.join(unexpected)}")
    return dict(sorted(evidence.items()))


def _markdown(manifest: dict[str, Any]) -> str:
    rows = [
        "# HL-Mem release evidence",
        "",
        f"- Version: `{manifest['version']}`",
        f"- Commit: `{manifest['commit']}`",
        f"- Workflow: [GitHub Actions run]({manifest['run_url']})",
        "",
        "| Gate | Status | Artifact | SHA-256 |",
        "|---|---|---|---|",
    ]
    for name, item in manifest["evidence"].items():
        rows.append(f"| `{name}` | {item['status']} | `{item['artifact']}` | `{item['sha256']}` |")
    return "\n".join(rows) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--junit", action="append", default=[])
    parser.add_argument("--json", action="append", default=[])
    parser.add_argument("--file", action="append", default=[])
    arguments = parser.parse_args(argv)

    try:
        if not COMMIT_PATTERN.fullmatch(arguments.commit):
            raise ValueError("commit must be a lowercase 40-hex Git object ID")
        if not arguments.version.strip():
            raise ValueError("version must not be empty")
        if not arguments.run_url.startswith("https://github.com/") or "/actions/runs/" not in arguments.run_url:
            raise ValueError("run URL must identify a GitHub Actions run")
        evidence = _collect(arguments.junit, arguments.json, arguments.file)
    except (ET.ParseError, json.JSONDecodeError, OSError, TypeError, ValueError) as error:
        print(f"Release evidence validation failed: {error}")
        return 1

    manifest = {
        "schema_version": 1,
        "version": arguments.version,
        "commit": arguments.commit,
        "run_url": arguments.run_url,
        "evidence": evidence,
    }
    arguments.output_dir.mkdir(parents=True, exist_ok=True)
    (arguments.output_dir / "release-evidence.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (arguments.output_dir / "release-evidence.md").write_text(_markdown(manifest), encoding="utf-8")
    print(f"Release evidence valid: {len(evidence)} gates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
