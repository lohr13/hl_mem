"""Bounded evidence about the HL-Mem runtime loaded by Hermes."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, Mapping

RUNTIME_STATUS_SCHEMA_VERSION = 1
MAX_FAILURE_COUNT = 10_000
RuntimeRegistrationState = Literal["registered", "registration_failed"]
_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40,64}$")
_EXCEPTION_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")
_STATUS_KEYS = {
    "schema_version",
    "package_version",
    "package_path",
    "git_sha",
    "pid",
    "observed_at",
    "status",
    "failure_count",
    "exception_type",
}


@dataclass(frozen=True)
class RuntimeIdentity:
    """Immutable identity of one imported HL-Mem package."""

    package_version: str
    package_path: str
    git_sha: str | None


@dataclass(frozen=True)
class RuntimeRegistration:
    """Last bounded registration result written by a Hermes process."""

    schema_version: int
    package_version: str
    package_path: str
    git_sha: str | None
    pid: int
    observed_at: str
    status: RuntimeRegistrationState
    failure_count: int
    exception_type: str | None

    @property
    def identity(self) -> RuntimeIdentity:
        return RuntimeIdentity(self.package_version, self.package_path, self.git_sha)


def runtime_status_path(hermes_home: str | Path) -> Path:
    """Return the single runtime evidence path owned by the Hermes home."""
    return Path(hermes_home).expanduser().resolve() / "state" / "hl_mem-runtime.json"


def _git_dir(marker: Path) -> Path | None:
    if marker.is_dir():
        return marker
    if not marker.is_file():
        return None
    try:
        declaration = marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    prefix = "gitdir:"
    if not declaration.lower().startswith(prefix):
        return None
    value = declaration[len(prefix) :].strip()
    if not value:
        return None
    candidate = Path(value)
    return (marker.parent / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()


def _read_git_sha(package_path: Path) -> str | None:
    for directory in (package_path, *package_path.parents):
        git_dir = _git_dir(directory / ".git")
        if git_dir is None:
            continue
        try:
            head = (git_dir / "HEAD").read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            return None
        if _SHA_PATTERN.fullmatch(head):
            return head.lower()
        if not head.startswith("ref: "):
            return None
        reference = head[5:].strip()
        try:
            resolved = (git_dir / reference).read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            resolved = ""
        if _SHA_PATTERN.fullmatch(resolved):
            return resolved.lower()
        try:
            packed_refs = (git_dir / "packed-refs").read_text(encoding="ascii").splitlines()
        except (OSError, UnicodeError):
            return None
        for line in packed_refs:
            if line.startswith(("#", "^")):
                continue
            sha, _, name = line.partition(" ")
            if name == reference and _SHA_PATTERN.fullmatch(sha):
                return sha.lower()
        return None
    return None


def capture_runtime_identity(
    *,
    package_path: str | Path | None = None,
    package_version: str | None = None,
) -> RuntimeIdentity:
    """Capture package identity without invoking Git or inspecting processes."""
    if package_version is None:
        from hl_mem import __version__

        package_version = __version__
    resolved_path = (
        Path(package_path).expanduser().resolve()
        if package_path is not None
        else Path(__file__).resolve().parents[2]
    )
    return RuntimeIdentity(
        package_version=package_version,
        package_path=str(resolved_path),
        git_sha=_read_git_sha(resolved_path),
    )


def _parse_status(payload: object) -> RuntimeRegistration:
    if not isinstance(payload, Mapping) or set(payload) != _STATUS_KEYS:
        raise ValueError("invalid Hermes runtime status structure")
    try:
        registration = RuntimeRegistration(**payload)
    except TypeError as error:
        raise ValueError("invalid Hermes runtime status fields") from error
    if registration.schema_version != RUNTIME_STATUS_SCHEMA_VERSION:
        raise ValueError("unsupported Hermes runtime status schema")
    if registration.status not in {"registered", "registration_failed"}:
        raise ValueError("invalid Hermes runtime status state")
    if not registration.package_version or not registration.package_path:
        raise ValueError("invalid Hermes runtime status identity")
    if registration.git_sha is not None and not _SHA_PATTERN.fullmatch(registration.git_sha):
        raise ValueError("invalid Hermes runtime status Git identity")
    if type(registration.pid) is not int or registration.pid < 1:
        raise ValueError("invalid Hermes runtime status process id")
    if type(registration.failure_count) is not int or not 0 <= registration.failure_count <= MAX_FAILURE_COUNT:
        raise ValueError("invalid Hermes runtime status failure count")
    try:
        datetime.fromisoformat(registration.observed_at)
    except (TypeError, ValueError) as error:
        raise ValueError("invalid Hermes runtime status timestamp") from error
    if registration.exception_type is not None and not _EXCEPTION_PATTERN.fullmatch(registration.exception_type):
        raise ValueError("invalid Hermes runtime status exception type")
    if registration.status == "registered" and (
        registration.failure_count != 0 or registration.exception_type is not None
    ):
        raise ValueError("invalid successful Hermes runtime status")
    if registration.status == "registration_failed" and registration.exception_type is None:
        raise ValueError("invalid failed Hermes runtime status")
    return registration


def read_runtime_status(hermes_home: str | Path) -> RuntimeRegistration | None:
    """Read and validate the last runtime registration result."""
    path = runtime_status_path(hermes_home)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid Hermes runtime status document") from error
    return _parse_status(payload)


def _previous_failure_count(hermes_home: str | Path) -> int:
    try:
        previous = read_runtime_status(hermes_home)
    except (OSError, ValueError):
        return 0
    return previous.failure_count if previous is not None else 0


def write_runtime_status(
    hermes_home: str | Path,
    identity: RuntimeIdentity,
    *,
    status: RuntimeRegistrationState,
    exception_type: str | None = None,
    now: Callable[[], datetime] | None = None,
    pid: int | None = None,
) -> RuntimeRegistration:
    """Atomically store bounded registration evidence without exception text."""
    if status == "registration_failed":
        if exception_type is None or not _EXCEPTION_PATTERN.fullmatch(exception_type):
            raise ValueError("a safe exception type is required for failed registration")
        failure_count = min(MAX_FAILURE_COUNT, _previous_failure_count(hermes_home) + 1)
    else:
        exception_type = None
        failure_count = 0
    observed_at = (now or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc).isoformat()
    registration = RuntimeRegistration(
        schema_version=RUNTIME_STATUS_SCHEMA_VERSION,
        package_version=identity.package_version,
        package_path=identity.package_path,
        git_sha=identity.git_sha,
        pid=pid if pid is not None else os.getpid(),
        observed_at=observed_at,
        status=status,
        failure_count=failure_count,
        exception_type=exception_type,
    )
    _parse_status(asdict(registration))
    target = runtime_status_path(hermes_home)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix="hl_mem-runtime.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            json.dump(asdict(registration), stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, target)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return registration
