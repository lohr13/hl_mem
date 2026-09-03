from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import write_release_evidence


def test_required_evidence_matches_consolidated_release_gates() -> None:
    assert write_release_evidence.REQUIRED_EVIDENCE == frozenset(
        {
            "python-3.13",
            "release-only",
            "public-recall",
            "pip-audit",
            "sbom",
            "wheel-install",
        }
    )


def _junit(path: Path, *, failures: int = 0, errors: int = 0) -> None:
    path.write_text(
        f'<testsuite name="gate" tests="1" failures="{failures}" errors="{errors}" skipped="0"/>',
        encoding="utf-8",
    )


def _arguments(output: Path, evidence: Path, *, omitted: str | None = None) -> list[str]:
    public_recall = evidence.with_name("public-recall.json")
    public_recall.write_text(
        json.dumps(
            {
                "gate_status": "passed",
                "case_count": 1,
                "http_success_rate": 1.0,
                "total_forbidden_hits": 0,
            }
        ),
        encoding="utf-8",
    )
    pip_audit = evidence.with_name("pip-audit.json")
    pip_audit.write_text(json.dumps({"dependencies": []}), encoding="utf-8")
    sbom = evidence.with_name("sbom.cdx.json")
    sbom.write_text(json.dumps({"bomFormat": "CycloneDX"}), encoding="utf-8")
    arguments = [
        "--version",
        "1.0.0rc1",
        "--commit",
        "a" * 40,
        "--run-url",
        "https://github.com/lohr13/hl_mem/actions/runs/123",
        "--output-dir",
        str(output),
    ]
    for name in sorted(write_release_evidence.REQUIRED_EVIDENCE):
        if name == omitted:
            continue
        if name == "public-recall":
            arguments.extend(("--json", f"{name}={public_recall}"))
        elif name == "pip-audit":
            arguments.extend(("--json", f"{name}={pip_audit}"))
        elif name == "sbom":
            arguments.extend(("--file", f"{name}={sbom}"))
        else:
            arguments.extend(("--junit", f"{name}={evidence}"))
    return arguments


def test_complete_passing_evidence_writes_json_and_markdown(tmp_path: Path) -> None:
    evidence = tmp_path / "passing.xml"
    output = tmp_path / "release"
    _junit(evidence)

    assert write_release_evidence.main(_arguments(output, evidence)) == 0

    manifest = json.loads((output / "release-evidence.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "1.0.0rc1"
    assert manifest["commit"] == "a" * 40
    assert set(manifest["evidence"]) == write_release_evidence.REQUIRED_EVIDENCE
    assert all(item["status"] == "passed" for item in manifest["evidence"].values())
    assert "actions/runs/123" in (output / "release-evidence.md").read_text(encoding="utf-8")


def test_missing_required_evidence_fails_without_success_manifest(tmp_path: Path) -> None:
    evidence = tmp_path / "passing.xml"
    output = tmp_path / "release"
    _junit(evidence)

    assert write_release_evidence.main(_arguments(output, evidence, omitted="sbom")) == 1
    assert not (output / "release-evidence.json").exists()


def test_failing_junit_fails_without_success_manifest(tmp_path: Path) -> None:
    evidence = tmp_path / "failing.xml"
    output = tmp_path / "release"
    _junit(evidence, failures=1)

    assert write_release_evidence.main(_arguments(output, evidence)) == 1
    assert not (output / "release-evidence.json").exists()


def test_pip_audit_list_rejects_vulnerabilities(tmp_path: Path) -> None:
    audit = tmp_path / "pip-audit.json"
    audit.write_text(
        json.dumps([{"name": "example", "version": "1.0", "vulns": [{"id": "CVE-TEST"}]}]),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="1 vulnerabilities"):
        write_release_evidence._json_status("pip-audit", audit)


def test_pip_audit_list_accepts_zero_vulnerabilities(tmp_path: Path) -> None:
    audit = tmp_path / "pip-audit.json"
    audit.write_text(json.dumps([{"name": "example", "version": "1.0", "vulns": []}]), encoding="utf-8")

    assert write_release_evidence._json_status("pip-audit", audit) == {
        "dependencies": 1,
        "vulnerabilities": 0,
    }


def test_pip_audit_current_object_accepts_zero_vulnerabilities(tmp_path: Path) -> None:
    audit = tmp_path / "pip-audit.json"
    audit.write_text(
        json.dumps(
            {
                "dependencies": [{"name": "example", "version": "1.0", "vulns": []}],
                "fixes": [],
            }
        ),
        encoding="utf-8",
    )

    assert write_release_evidence.validate_pip_audit(audit) == {
        "dependencies": 1,
        "vulnerabilities": 0,
    }


def test_pip_audit_current_object_rejects_vulnerabilities(tmp_path: Path) -> None:
    audit = tmp_path / "pip-audit.json"
    audit.write_text(
        json.dumps(
            {
                "dependencies": [{"name": "example", "version": "1.0", "vulns": [{"id": "CVE-TEST"}]}],
                "fixes": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="1 vulnerabilities"):
        write_release_evidence.validate_pip_audit(audit)
