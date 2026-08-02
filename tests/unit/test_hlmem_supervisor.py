"""Behavior tests for the Windows HL-Mem supervisor."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from unittest.mock import Mock, call, patch

import pytest

from scripts import hlmem_supervisor as supervisor


def _config(tmp_path: Path, **overrides: object) -> supervisor.SupervisorConfig:
    values: dict[str, object] = {
        "repo_root": tmp_path,
        "state_file": tmp_path / "var" / "supervisor.state",
        "log_file": tmp_path / "var" / "supervisor.log",
        "lock_dir": tmp_path / "var" / "supervisor.lock",
        "start_script": tmp_path / "missing-start-script.sh",
        "bash_executable": tmp_path / "Git" / "bin" / "bash.exe",
        "termination_wait_seconds": 0.0,
        "termination_poll_seconds": 0.0,
        "start_grace_seconds": 0.0,
    }
    values.update(overrides)
    return supervisor.SupervisorConfig(**values)


def _read_state(config: supervisor.SupervisorConfig) -> dict[str, int]:
    return json.loads(config.state_file.read_text(encoding="utf-8"))


def _running_process(pid: int = 2468) -> Mock:
    process = Mock(pid=pid)
    process.poll.return_value = None
    return process


def test_failed_probe_increments_persisted_failure_count(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runner = supervisor.Supervisor(
        config,
        probe_fn=lambda _url, _timeout: (False, "connection refused"),
        restart_fn=Mock(),
        clock=lambda: 1_000,
    )

    assert runner.run_once() == 1
    assert _read_state(config) == {"failures": 1, "last_restart_epoch": 0}


def test_successful_probe_clears_persisted_failure_count(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.state_file.parent.mkdir(parents=True)
    config.state_file.write_text(
        json.dumps({"failures": 2, "last_restart_epoch": 900}),
        encoding="utf-8",
    )
    runner = supervisor.Supervisor(
        config,
        probe_fn=lambda _url, _timeout: (True, "status=ok"),
        restart_fn=Mock(),
        clock=lambda: 1_000,
    )

    assert runner.run_once() == 0
    assert _read_state(config) == {"failures": 0, "last_restart_epoch": 900}
    assert "probe ok" in config.log_file.read_text(encoding="utf-8")


def test_third_consecutive_failure_triggers_restart(tmp_path: Path) -> None:
    config = _config(tmp_path)
    restart = Mock(return_value=4321)

    for now in (1_000, 1_001, 1_002):
        runner = supervisor.Supervisor(
            config,
            probe_fn=lambda _url, _timeout: (False, "unhealthy"),
            restart_fn=restart,
            clock=lambda now=now: now,
        )
        assert runner.run_once() == 1

    restart.assert_called_once_with()
    assert _read_state(config) == {"failures": 0, "last_restart_epoch": 1_002}
    assert "restart triggered" in config.log_file.read_text(encoding="utf-8")


def test_restart_is_suppressed_during_cooldown(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.state_file.parent.mkdir(parents=True)
    config.state_file.write_text(
        json.dumps({"failures": 2, "last_restart_epoch": 970}),
        encoding="utf-8",
    )
    restart = Mock()
    runner = supervisor.Supervisor(
        config,
        probe_fn=lambda _url, _timeout: (False, "unhealthy"),
        restart_fn=restart,
        clock=lambda: 1_000,
    )

    assert runner.run_once() == 1
    restart.assert_not_called()
    assert _read_state(config) == {"failures": 3, "last_restart_epoch": 970}
    log = config.log_file.read_text(encoding="utf-8")
    assert "restart suppressed" in log
    assert "remaining_seconds=30" in log


def test_cooldown_timestamp_is_recorded_after_restart_finishes(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.state_file.parent.mkdir(parents=True)
    config.state_file.write_text(
        json.dumps({"failures": 2, "last_restart_epoch": 0}),
        encoding="utf-8",
    )
    clock_values = iter((1_000, 1_000, 1_065))
    runner = supervisor.Supervisor(
        config,
        probe_fn=lambda _url, _timeout: (False, "unhealthy"),
        restart_fn=Mock(return_value=4321),
        clock=lambda: next(clock_values),
    )

    assert runner.run_once() == 1
    assert _read_state(config) == {"failures": 0, "last_restart_epoch": 1_065}


def test_restart_failure_preserves_failure_count(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.state_file.parent.mkdir(parents=True)
    config.state_file.write_text(
        json.dumps({"failures": 2, "last_restart_epoch": 0}),
        encoding="utf-8",
    )
    runner = supervisor.Supervisor(
        config,
        probe_fn=lambda _url, _timeout: (False, "unhealthy"),
        restart_fn=Mock(side_effect=RuntimeError("launcher exited")),
        clock=lambda: 1_000,
    )

    assert runner.run_once() == 1
    assert _read_state(config) == {"failures": 3, "last_restart_epoch": 0}
    assert "restart failed" in config.log_file.read_text(encoding="utf-8")


def test_direct_restart_is_silent_and_sets_service_environment(tmp_path: Path) -> None:
    config = _config(tmp_path)
    netstat = subprocess.CompletedProcess(
        args=["netstat.exe"],
        returncode=0,
        stdout="",
        stderr="",
    )
    process = _running_process()

    with (
        patch.object(supervisor.subprocess, "run", return_value=netstat) as run,
        patch.object(supervisor.subprocess, "Popen", return_value=process) as popen,
    ):
        assert supervisor.restart_service(config, Mock()) == 2468

    assert run.call_args.kwargs["creationflags"] & supervisor.CREATE_NO_WINDOW
    assert run.call_args.kwargs["timeout"] == config.command_timeout_seconds
    command = popen.call_args.args[0]
    options = popen.call_args.kwargs
    assert command == [
        str(tmp_path / ".venv" / "Scripts" / "python.exe"),
        "start_server.py",
    ]
    assert options["cwd"] == str(tmp_path)
    assert options["creationflags"] & supervisor.CREATE_NO_WINDOW
    assert options["stdin"] is subprocess.DEVNULL
    assert options["stdout"] is subprocess.DEVNULL
    assert options["stderr"] is subprocess.DEVNULL
    assert options["env"]["VIRTUAL_ENV"] == str(tmp_path / ".venv")
    assert options["env"]["PYTHONPATH"] == str(tmp_path / "src")


def test_restart_kills_port_listener_without_a_window(tmp_path: Path) -> None:
    config = _config(tmp_path)
    netstat = subprocess.CompletedProcess(
        args=["netstat.exe"],
        returncode=0,
        stdout=(
            "  TCP    127.0.0.1:8200    0.0.0.0:0    LISTENING    111\n"
            "  TCP    [::1]:8200        [::]:0       LISTENING    222\n"
            "  TCP    127.0.0.1:9999    0.0.0.0:0    LISTENING    333\n"
        ),
        stderr="",
    )
    killed = subprocess.CompletedProcess(args=["taskkill.exe"], returncode=0)
    port_released = subprocess.CompletedProcess(
        args=["netstat.exe"],
        returncode=0,
        stdout="",
        stderr="",
    )

    with (
        patch.object(supervisor.subprocess, "run", side_effect=[netstat, killed, killed, port_released]) as run,
        patch.object(supervisor.subprocess, "Popen", return_value=_running_process()),
    ):
        supervisor.restart_service(config, Mock())

    assert run.call_args_list[1] == call(
        ["taskkill.exe", "/PID", "111", "/T", "/F"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        creationflags=supervisor.CREATE_NO_WINDOW,
        timeout=config.command_timeout_seconds,
    )
    assert run.call_args_list[2] == call(
        ["taskkill.exe", "/PID", "222", "/T", "/F"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        creationflags=supervisor.CREATE_NO_WINDOW,
        timeout=config.command_timeout_seconds,
    )
    assert run.call_args_list[3].kwargs["creationflags"] & supervisor.CREATE_NO_WINDOW
    assert run.call_args_list[3].kwargs["timeout"] == config.command_timeout_seconds


def test_restart_aborts_when_port_does_not_become_free(tmp_path: Path) -> None:
    config = _config(tmp_path)
    occupied = subprocess.CompletedProcess(
        args=["netstat.exe"],
        returncode=0,
        stdout="  TCP    127.0.0.1:8200    0.0.0.0:0    LISTENING    111\n",
        stderr="",
    )
    killed = subprocess.CompletedProcess(args=["taskkill.exe"], returncode=0)

    with (
        patch.object(supervisor.subprocess, "run", side_effect=[occupied, killed, occupied]),
        patch.object(supervisor.subprocess, "Popen") as popen,
        pytest.raises(RuntimeError, match="port 8200 is still occupied"),
    ):
        supervisor.restart_service(config, Mock())

    popen.assert_not_called()


def test_existing_start_script_uses_absolute_bash_without_a_window(tmp_path: Path) -> None:
    start_script = tmp_path / "bin" / "start_hlmem.sh"
    start_script.parent.mkdir()
    start_script.write_text("#!/bin/bash\n", encoding="utf-8")
    bash = tmp_path / "Git" / "bin" / "bash.exe"
    config = _config(tmp_path, start_script=start_script, bash_executable=bash)
    netstat = subprocess.CompletedProcess(
        args=["netstat.exe"],
        returncode=0,
        stdout="",
        stderr="",
    )

    with (
        patch.object(supervisor.subprocess, "run", return_value=netstat),
        patch.object(supervisor.subprocess, "Popen", return_value=_running_process()) as popen,
    ):
        supervisor.restart_service(config, Mock())

    assert popen.call_args.args[0] == [
        str(bash.absolute()),
        "--noprofile",
        "--norc",
        str(start_script),
    ]
    assert popen.call_args.kwargs["creationflags"] & supervisor.CREATE_NO_WINDOW


def test_immediately_exited_launcher_is_not_a_successful_restart(tmp_path: Path) -> None:
    config = _config(tmp_path)
    netstat = subprocess.CompletedProcess(
        args=["netstat.exe"],
        returncode=0,
        stdout="",
        stderr="",
    )
    process = Mock(pid=2468)
    process.poll.return_value = 1

    with (
        patch.object(supervisor.subprocess, "run", return_value=netstat),
        patch.object(supervisor.subprocess, "Popen", return_value=process),
        pytest.raises(RuntimeError, match="launcher exited immediately with code 1"),
    ):
        supervisor.restart_service(config, Mock())


def test_fresh_lock_prevents_overlapping_probe(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.lock_dir.mkdir(parents=True)
    probe = Mock(return_value=(True, "status=ok"))
    runner = supervisor.Supervisor(config, probe_fn=probe, clock=lambda: 1_000)

    assert runner.run_once() == 0
    probe.assert_not_called()
    assert "already running" in config.log_file.read_text(encoding="utf-8")


def test_stale_lock_is_reclaimed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.lock_dir.mkdir(parents=True)
    supervisor.os.utime(config.lock_dir, (600, 600))
    probe = Mock(return_value=(True, "status=ok"))
    runner = supervisor.Supervisor(config, probe_fn=probe, clock=lambda: 1_000)

    assert runner.run_once() == 0
    probe.assert_called_once_with(config.url, config.timeout)
    assert not config.lock_dir.exists()
    log = config.log_file.read_text(encoding="utf-8")
    assert "stale lock reclaimed" in log
    assert "probe ok" in log


def test_reclaim_guard_serializes_competing_reclaimers(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first_guard = supervisor._ReclaimGuard(config.lock_dir)
    assert first_guard.acquire() is True
    competing_result: list[bool] = []

    def compete() -> None:
        competing = supervisor._ReclaimGuard(config.lock_dir)
        competing_result.append(competing.acquire())
        competing.release()

    competitor = threading.Thread(target=compete)
    competitor.start()
    competitor.join(timeout=5)

    assert not competitor.is_alive()
    assert competing_result == [False]
    first_guard.release()

    next_guard = supervisor._ReclaimGuard(config.lock_dir)
    assert next_guard.acquire() is True
    next_guard.release()


def test_stale_lock_with_live_owner_is_not_reclaimed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    probe_started = threading.Event()
    release_probe = threading.Event()

    def blocking_probe(_url: str, _timeout: float) -> tuple[bool, str]:
        probe_started.set()
        assert release_probe.wait(timeout=5)
        return True, "status=ok"

    first = supervisor.Supervisor(config, probe_fn=blocking_probe, clock=lambda: 1_000)
    first_thread = threading.Thread(target=first.run_once)
    first_thread.start()
    assert probe_started.wait(timeout=5)
    os.utime(config.lock_dir, (600, 600))
    second_probe = Mock(return_value=(True, "status=ok"))

    try:
        assert supervisor.Supervisor(config, probe_fn=second_probe, clock=lambda: 1_000).run_once() == 0
        second_probe.assert_not_called()
    finally:
        release_probe.set()
        first_thread.join(timeout=5)

    assert not first_thread.is_alive()


def test_lock_owner_only_releases_its_own_token(tmp_path: Path) -> None:
    config = _config(tmp_path)

    def replace_lock_owner(_url: str, _timeout: float) -> tuple[bool, str]:
        owner_file = config.lock_dir / supervisor.LOCK_OWNER_FILENAME
        owner = json.loads(owner_file.read_text(encoding="utf-8"))
        owner["token"] = "replacement-owner"
        owner_file.write_text(json.dumps(owner), encoding="utf-8")
        return True, "status=ok"

    runner = supervisor.Supervisor(config, probe_fn=replace_lock_owner, clock=lambda: 1_000)

    assert runner.run_once() == 0
    assert config.lock_dir.exists()
    owner = json.loads((config.lock_dir / supervisor.LOCK_OWNER_FILENAME).read_text(encoding="utf-8"))
    assert owner["token"] == "replacement-owner"


def test_relative_start_script_is_resolved_from_repo_root(tmp_path: Path) -> None:
    with patch.dict(
        supervisor.os.environ,
        {"HL_MEM_ROOT": str(tmp_path), "HL_MEM_START_SCRIPT": "bin/start_hlmem.sh"},
    ):
        config = supervisor.build_config()

    assert config.start_script == (tmp_path / "bin" / "start_hlmem.sh").absolute()
