"""Fail-closed runtime identity checks for paid evaluation runners."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import hl_mem
from hl_mem.config.secrets import read_secret_values
from hl_mem.ingest.llm_extractor import LLM_EXTRACTOR_VERSION

EXPECTED_GIT_HEAD_ENV = "HL_MEM_EVAL_EXPECTED_GIT_HEAD"
EXPECTED_REPO_ROOT_ENV = "HL_MEM_EVAL_EXPECTED_REPO_ROOT"
EXPECTED_EXTRACTOR_VERSION_ENV = "HL_MEM_EVAL_EXPECTED_EXTRACTOR_VERSION"
EXPECTED_CONFIG_SHA256_ENV = "HL_MEM_EVAL_EXPECTED_CONFIG_SHA256"
_EXPECTED_ENV_NAMES = (
    EXPECTED_GIT_HEAD_ENV,
    EXPECTED_REPO_ROOT_ENV,
    EXPECTED_EXTRACTOR_VERSION_ENV,
    EXPECTED_CONFIG_SHA256_ENV,
)


class EvaluationIdentityError(RuntimeError):
    """Raised before or after a paid run when its identity is not exact."""

    def __init__(
        self,
        message: str,
        *,
        checks: Mapping[str, bool] | None = None,
        manifest_count: int | None = None,
        matching_manifest_count: int | None = None,
    ) -> None:
        super().__init__(message)
        self.checks = dict(checks or {})
        self.manifest_count = manifest_count
        self.matching_manifest_count = matching_manifest_count


@dataclass(frozen=True)
class RunIdentityResult:
    """Safe-to-log preflight result; it intentionally contains no API key."""

    expected_extractor_version: str
    checks: dict[str, bool]


@dataclass(frozen=True)
class ManifestIdentityResult:
    """Whole-run manifest identity summary."""

    manifest_count: int
    matching_manifest_count: int
    checks: dict[str, bool]


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _git_head(repo_root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip()


def _is_below(path_value: str | None, prefix: Path) -> bool:
    if not path_value:
        return False
    try:
        return Path(path_value).resolve().is_relative_to(prefix.resolve())
    except OSError:
        return False


def _module_origin(module_name: str) -> str | None:
    """Resolve import identity without executing the target module."""

    try:
        spec = importlib.util.find_spec(module_name)
    except (AttributeError, ImportError, ValueError):
        return None
    return spec.origin if spec is not None else None


def _print_checks(stage: str, checks: Mapping[str, bool]) -> None:
    print(
        json.dumps(
            {"evaluation_identity_gate": stage, "checks": dict(checks)},
            sort_keys=True,
        ),
        flush=True,
    )


def _raise_for_failed_checks(
    stage: str,
    checks: Mapping[str, bool],
    *,
    manifest_count: int | None = None,
    matching_manifest_count: int | None = None,
) -> None:
    failed = [f"{name}=False" for name, passed in checks.items() if not passed]
    if failed:
        raise EvaluationIdentityError(
            f"evaluation identity {stage} failed: {', '.join(failed)}",
            checks=checks,
            manifest_count=manifest_count,
            matching_manifest_count=matching_manifest_count,
        )


def assert_run_identity_from_env(
    *,
    config_path: Path,
    env_path: Path,
    required: bool,
    environ: Mapping[str, str] | None = None,
) -> RunIdentityResult | None:
    """Assert the imported code, checkout, config, and API-key class before a run.

    When ``required`` is false, an entirely absent expectation set is a no-op so
    importing runners and executing local unit tests remain free of deployment
    setup. A partial expectation set is always rejected.
    """

    effective_environ = os.environ if environ is None else environ
    expected = {name: effective_environ.get(name, "").strip() for name in _EXPECTED_ENV_NAMES}
    supplied = {name for name, value in expected.items() if value}
    if not supplied and not required:
        return None
    missing = [name for name in _EXPECTED_ENV_NAMES if not expected[name]]
    if missing:
        raise EvaluationIdentityError("missing required identity expectations: " + ", ".join(missing))

    repo_root = Path(expected[EXPECTED_REPO_ROOT_ENV]).resolve()
    source_root = repo_root / "src"
    tests_root = repo_root / "tests"
    chinese_e2e_origin = _module_origin("tests.eval.chinese_e2e")
    secrets = read_secret_values(env_path, {"LLM_API_KEY"}, effective_environ)
    api_key = secrets.get("LLM_API_KEY", "")
    checks = {
        "git_head": _git_head(repo_root) == expected[EXPECTED_GIT_HEAD_ENV],
        "hl_mem_path": _is_below(hl_mem.__file__, source_root),
        "chinese_e2e_path": _is_below(chinese_e2e_origin, tests_root),
        "extractor_version": LLM_EXTRACTOR_VERSION == expected[EXPECTED_EXTRACTOR_VERSION_ENV],
        "config_sha256": _sha256(config_path) == expected[EXPECTED_CONFIG_SHA256_ENV].lower(),
        "llm_api_key_prefix": api_key.startswith("sk-sp-"),
    }
    _print_checks("preflight", checks)
    _raise_for_failed_checks("preflight", checks)
    return RunIdentityResult(
        expected_extractor_version=expected[EXPECTED_EXTRACTOR_VERSION_ENV],
        checks=checks,
    )


def _report_manifest_paths(report: Mapping[str, object]) -> tuple[Path, ...]:
    raw_cases = report.get("cases")
    if not isinstance(raw_cases, Sequence) or isinstance(raw_cases, (str, bytes)):
        return ()
    unique: dict[str, Path] = {}
    for raw_case in raw_cases:
        if not isinstance(raw_case, Mapping):
            continue
        ingest = raw_case.get("ingest")
        if not isinstance(ingest, Mapping):
            continue
        raw_path = ingest.get("cache_manifest")
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        path = Path(raw_path).resolve()
        unique[str(path)] = path
    return tuple(unique.values())


def assert_report_manifest_identity(
    report: Mapping[str, object],
    *,
    expected_extractor_version: str,
    expected_manifest_count: int,
) -> ManifestIdentityResult:
    """Require every unique cache manifest in a completed report to match."""

    paths = _report_manifest_paths(report)
    matching = 0
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, Mapping) and payload.get("extractor_version") == expected_extractor_version:
            matching += 1
    checks = {
        "manifest_count": len(paths) == expected_manifest_count,
        "manifest_extractor_versions": matching == expected_manifest_count,
    }
    _print_checks("postflight", checks)
    _raise_for_failed_checks(
        "postflight",
        checks,
        manifest_count=len(paths),
        matching_manifest_count=matching,
    )
    return ManifestIdentityResult(
        manifest_count=len(paths),
        matching_manifest_count=matching,
        checks=checks,
    )


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _set_report_gate_status(
    report: MutableMapping[str, object],
    *,
    valid: bool,
    checks: Mapping[str, bool],
    expected_manifest_count: int,
    manifest_count: int,
    matching_manifest_count: int,
) -> None:
    raw_run = report.get("run")
    if not isinstance(raw_run, MutableMapping):
        raw_run = {}
        report["run"] = raw_run
    raw_run["identity_gate"] = {
        "valid": valid,
        "checks": dict(checks),
        "expected_manifest_count": expected_manifest_count,
        "manifest_count": manifest_count,
        "matching_manifest_count": matching_manifest_count,
    }


def finalize_report_identity(
    report: MutableMapping[str, object],
    *,
    report_path: Path,
    expected_extractor_version: str,
    expected_manifest_count: int,
) -> ManifestIdentityResult:
    """Persist postflight evidence and fail closed with ``status=invalid``."""

    try:
        result = assert_report_manifest_identity(
            report,
            expected_extractor_version=expected_extractor_version,
            expected_manifest_count=expected_manifest_count,
        )
    except EvaluationIdentityError as error:
        report["status"] = "invalid"
        _set_report_gate_status(
            report,
            valid=False,
            checks=error.checks,
            expected_manifest_count=expected_manifest_count,
            manifest_count=0 if error.manifest_count is None else error.manifest_count,
            matching_manifest_count=(0 if error.matching_manifest_count is None else error.matching_manifest_count),
        )
        _write_json_atomic(report_path, report)
        raise

    _set_report_gate_status(
        report,
        valid=True,
        checks=result.checks,
        expected_manifest_count=expected_manifest_count,
        manifest_count=result.manifest_count,
        matching_manifest_count=result.matching_manifest_count,
    )
    _write_json_atomic(report_path, report)
    return result
