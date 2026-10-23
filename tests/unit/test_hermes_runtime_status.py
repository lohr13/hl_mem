"""Hermes loaded-runtime identity and registration evidence tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import hl_mem.adapters.hermes.runtime_status as runtime_status
from hl_mem.adapters.hermes.runtime_status import (
    MAX_FAILURE_COUNT,
    RuntimeIdentity,
    capture_runtime_identity,
    read_runtime_status,
    runtime_status_path,
    write_runtime_status,
)


def _identity(path: Path, *, version: str = "1.1.0", git_sha: str | None = "a" * 40) -> RuntimeIdentity:
    return RuntimeIdentity(package_version=version, package_path=str(path.resolve()), git_sha=git_sha)


def test_capture_runtime_identity_reads_editable_checkout_git_head(tmp_path: Path) -> None:
    package = tmp_path / "repo" / "src" / "hl_mem"
    package.mkdir(parents=True)
    git_dir = tmp_path / "repo" / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/develop\n", encoding="utf-8")
    (git_dir / "refs" / "heads" / "develop").write_text("b" * 40 + "\n", encoding="ascii")

    identity = capture_runtime_identity(package_path=package, package_version="1.1.0")

    assert identity == _identity(package, git_sha="b" * 40)


def test_capture_runtime_identity_treats_wheel_as_versioned_without_git(tmp_path: Path) -> None:
    package = tmp_path / "site-packages" / "hl_mem"
    package.mkdir(parents=True)

    identity = capture_runtime_identity(package_path=package, package_version="1.1.0")

    assert identity == _identity(package, git_sha=None)


def test_runtime_status_round_trip_is_atomic_and_contains_no_error_message(tmp_path: Path) -> None:
    identity = _identity(tmp_path / "package")
    secret = "https://user:password@example.test/?token=private"

    written = write_runtime_status(
        tmp_path,
        identity,
        status="registration_failed",
        exception_type="RuntimeError",
        now=lambda: datetime(2026, 9, 1, tzinfo=timezone.utc),
        pid=123,
    )

    assert read_runtime_status(tmp_path) == written
    payload = runtime_status_path(tmp_path).read_text(encoding="utf-8")
    assert secret not in payload
    assert "RuntimeError" in payload
    assert list((tmp_path / "state").glob("*.tmp")) == []


def test_success_resets_failure_count_and_failure_count_is_bounded(tmp_path: Path) -> None:
    identity = _identity(tmp_path / "package")
    path = runtime_status_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "package_version": identity.package_version,
                "package_path": identity.package_path,
                "git_sha": identity.git_sha,
                "pid": 1,
                "observed_at": "2026-09-01T00:00:00+00:00",
                "status": "registration_failed",
                "failure_count": MAX_FAILURE_COUNT,
                "exception_type": "RuntimeError",
            }
        ),
        encoding="utf-8",
    )

    failed = write_runtime_status(tmp_path, identity, status="registration_failed", exception_type="ValueError")
    succeeded = write_runtime_status(tmp_path, identity, status="registered")

    assert failed.failure_count == MAX_FAILURE_COUNT
    assert succeeded.failure_count == 0
    assert succeeded.exception_type is None


def test_runtime_status_rejects_malformed_or_unexpected_payload(tmp_path: Path) -> None:
    path = runtime_status_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"schema_version": 1, "secret": "must-not-surface"}', encoding="utf-8")

    with pytest.raises(ValueError, match="runtime status"):
        read_runtime_status(tmp_path)


def test_runtime_status_write_failure_leaves_no_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(runtime_status.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        write_runtime_status(tmp_path, _identity(tmp_path / "package"), status="registered")

    assert not runtime_status_path(tmp_path).exists()
    assert list((tmp_path / "state").glob("*.tmp")) == []
