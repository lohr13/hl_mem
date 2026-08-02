"""Behavior tests for the standalone HL-Mem watchdog."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest


WATCHDOG = Path(__file__).resolve().parents[2] / "scripts" / "hlmem_watchdog.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(0o755)


@pytest.fixture
def watchdog_environment(tmp_path: Path) -> tuple[dict[str, str], Path]:
    system_bash = shutil.which("bash")
    if system_bash is None:
        pytest.skip("watchdog behavior tests require Bash")

    root = tmp_path / "hl_mem"
    var_dir = root / "var"
    fake_bin = tmp_path / "fake-bin"
    var_dir.mkdir(parents=True)
    fake_bin.mkdir()
    (var_dir / "hl_mem.db").write_bytes(b"database")
    (var_dir / "hl_mem.db-wal").write_bytes(b"wal")
    (var_dir / "server.log").write_text("server tail\n", encoding="utf-8")
    start_script = tmp_path / "start_hlmem.sh"
    start_script.write_text("#!/usr/bin/env bash\n", encoding="utf-8", newline="\n")
    actions = tmp_path / "actions.log"

    _write_executable(
        fake_bin / "curl",
        """#!/usr/bin/env bash
printf 'curl %s\\n' "$*" >> "$HL_MEM_TEST_ACTIONS"
if [[ "${HL_MEM_TEST_CURL_EXIT:-28}" == "0" ]]; then
    printf '{"status":"ok"}'
    exit 0
fi
printf 'simulated timeout' >&2
exit "$HL_MEM_TEST_CURL_EXIT"
""",
    )
    _write_executable(
        fake_bin / "netstat.exe",
        """#!/usr/bin/env bash
if [[ -f "$HL_MEM_TEST_KILLED" ]]; then
    exit 0
fi
printf '  TCP    127.0.0.1:8200    0.0.0.0:0    LISTENING    4321\\n'
""",
    )
    _write_executable(
        fake_bin / "powershell.exe",
        """#!/usr/bin/env bash
printf 'powershell %s\\n' "$*" >> "$HL_MEM_TEST_ACTIONS"
sleep "${HL_MEM_TEST_POWERSHELL_SLEEP:-0}"
printf 'process diagnostics for 4321\\n'
""",
    )
    _write_executable(
        fake_bin / "tasklist.exe",
        """#!/usr/bin/env bash
printf 'python.exe 4321 Console 1 100,000 K\\n'
""",
    )
    _write_executable(
        fake_bin / "taskkill.exe",
        """#!/usr/bin/env bash
printf 'taskkill %s\\n' "$*" >> "$HL_MEM_TEST_ACTIONS"
if [[ "${HL_MEM_TEST_TASKKILL_EXIT:-0}" != "0" ]]; then
    exit "$HL_MEM_TEST_TASKKILL_EXIT"
fi
touch "$HL_MEM_TEST_KILLED"
""",
    )
    fake_start_bash = fake_bin / "start-bash"
    _write_executable(
        fake_start_bash,
        """#!/usr/bin/env bash
printf 'restart %s virtual_env=%s pythonpath=%s\\n' \
    "$*" "${VIRTUAL_ENV-unset}" "${PYTHONPATH-unset}" >> "$HL_MEM_TEST_ACTIONS"
if [[ "${HL_MEM_TEST_START_EXIT:-0}" != "0" ]]; then
    exit "$HL_MEM_TEST_START_EXIT"
fi
printf 'fake service output\\n'
sleep 2
""",
    )

    inherited_names = (
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "HOME",
        "USERPROFILE",
        "TEMP",
        "TMP",
    )
    env = {name: os.environ[name] for name in inherited_names if name in os.environ}
    env.update(
        {
            "HL_MEM_ROOT": root.as_posix(),
            "HL_MEM_START_SCRIPT": start_script.as_posix(),
            "HL_MEM_BASH": fake_start_bash.as_posix(),
            "HL_MEM_CURL": (fake_bin / "curl").as_posix(),
            "HL_MEM_NETSTAT": (fake_bin / "netstat.exe").as_posix(),
            "HL_MEM_POWERSHELL": (fake_bin / "powershell.exe").as_posix(),
            "HL_MEM_TASKLIST": (fake_bin / "tasklist.exe").as_posix(),
            "HL_MEM_TASKKILL": (fake_bin / "taskkill.exe").as_posix(),
            "HL_MEM_PY_SPY": "missing-py-spy-for-test",
            "HL_MEM_TEST_ACTIONS": actions.as_posix(),
            "HL_MEM_TEST_KILLED": (tmp_path / "killed.marker").as_posix(),
            "HL_MEM_TEST_CURL_EXIT": "28",
            "VIRTUAL_ENV": "hermes-contamination",
            "PYTHONPATH": "hermes-contamination",
        }
    )
    return env, actions


def _invoke_watchdog(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    system_bash = shutil.which("bash")
    assert system_bash is not None
    return subprocess.run(
        [system_bash, WATCHDOG.as_posix()],
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )


def _run_watchdog(env: dict[str, str]) -> None:
    result = _invoke_watchdog(env)
    assert result.returncode == 0, result.stderr


def _state(root: str) -> dict[str, int]:
    content = (Path(root) / "var" / "watchdog.state").read_text(encoding="utf-8")
    entries = (line.split("=", 1) for line in content.splitlines())
    return {key: int(value) for key, value in entries}


def _wait_for_restart(actions: Path) -> str:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        content = actions.read_text(encoding="utf-8") if actions.exists() else ""
        if "restart " in content:
            return content
        time.sleep(0.02)
    return actions.read_text(encoding="utf-8") if actions.exists() else ""


def test_watchdog_restarts_only_after_three_consecutive_failures(
    watchdog_environment: tuple[dict[str, str], Path],
) -> None:
    env, actions = watchdog_environment

    _run_watchdog(env)
    assert _state(env["HL_MEM_ROOT"])["failures"] == 1
    watchdog_log = Path(env["HL_MEM_ROOT"]) / "var" / "watchdog.log"
    assert "curl_exit=28" in watchdog_log.read_text(encoding="utf-8")
    _run_watchdog(env)
    assert _state(env["HL_MEM_ROOT"])["failures"] == 2
    assert "restart " not in actions.read_text(encoding="utf-8")

    _run_watchdog(env)

    state = _state(env["HL_MEM_ROOT"])
    assert state["failures"] == 0
    assert state["last_restart_epoch"] > 0
    action_log = _wait_for_restart(actions)
    assert "taskkill /PID 4321 /T /F" in action_log
    assert "restart --noprofile --norc" in action_log
    assert "virtual_env=unset pythonpath=unset" in action_log
    startup_log = Path(env["HL_MEM_ROOT"]) / "var" / "hlmem_startup.log"
    assert startup_log.read_text(encoding="utf-8") == "fake service output\n"
    packages = list((Path(env["HL_MEM_ROOT"]) / "var" / "crash-packages").iterdir())
    assert len(packages) == 1
    assert (packages[0] / "healthz-probes.log").is_file()
    assert (packages[0] / "port-listeners.txt").is_file()
    assert (packages[0] / "process-tree.txt").is_file()
    assert (packages[0] / "process-resources.txt").is_file()
    assert (packages[0] / "database-files.txt").is_file()
    assert (packages[0] / "service-logs-tail.txt").is_file()
    assert (packages[0] / "python-stacks.txt").is_file()


def test_successful_probe_resets_consecutive_failures(
    watchdog_environment: tuple[dict[str, str], Path],
) -> None:
    env, actions = watchdog_environment
    _run_watchdog(env)
    _run_watchdog(env)

    env["HL_MEM_TEST_CURL_EXIT"] = "0"
    _run_watchdog(env)

    assert _state(env["HL_MEM_ROOT"])["failures"] == 0
    assert "restart " not in actions.read_text(encoding="utf-8")


def test_watchdog_suppresses_another_restart_during_cooldown(
    watchdog_environment: tuple[dict[str, str], Path],
) -> None:
    env, actions = watchdog_environment
    env["HL_MEM_WATCHDOG_COOLDOWN_SECONDS"] = "3600"
    for _ in range(3):
        _run_watchdog(env)
    _wait_for_restart(actions)

    for _ in range(3):
        _run_watchdog(env)

    action_lines = actions.read_text(encoding="utf-8").splitlines()
    assert len([line for line in action_lines if line.startswith("restart ")]) == 1
    watchdog_log = Path(env["HL_MEM_ROOT"]) / "var" / "watchdog.log"
    assert "restart_suppressed reason=cooldown" in watchdog_log.read_text(encoding="utf-8")


def test_watchdog_reclaims_an_expired_lock(
    watchdog_environment: tuple[dict[str, str], Path],
) -> None:
    env, _ = watchdog_environment
    env["HL_MEM_WATCHDOG_LOCK_STALE_SECONDS"] = "1"
    lock_dir = Path(env["HL_MEM_ROOT"]) / "var" / "watchdog.lock"
    lock_dir.mkdir()
    (lock_dir / "owner").write_text("pid=999999\ncreated_epoch=1\n", encoding="utf-8")
    os.utime(lock_dir, (1, 1))

    _run_watchdog(env)

    assert _state(env["HL_MEM_ROOT"])["failures"] == 1
    assert not lock_dir.exists()
    watchdog_log = Path(env["HL_MEM_ROOT"]) / "var" / "watchdog.log"
    assert "stale_lock_reclaimed" in watchdog_log.read_text(encoding="utf-8")


def test_watchdog_does_not_reclaim_old_lock_from_live_owner(
    watchdog_environment: tuple[dict[str, str], Path],
    tmp_path: Path,
) -> None:
    env, _ = watchdog_environment
    system_bash = shutil.which("bash")
    assert system_bash is not None
    holder_pid_file = tmp_path / "holder.pid"
    holder = subprocess.Popen(
        [
            system_bash,
            "-c",
            'printf "%s\\n" "$$" > "$1"; sleep 30',
            "watchdog-lock-holder",
            holder_pid_file.as_posix(),
        ],
        env=env,
    )
    try:
        deadline = time.monotonic() + 2
        while not holder_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        owner_pid = holder_pid_file.read_text(encoding="utf-8").strip()
        env["HL_MEM_WATCHDOG_LOCK_STALE_SECONDS"] = "1"
        lock_dir = Path(env["HL_MEM_ROOT"]) / "var" / "watchdog.lock"
        lock_dir.mkdir()
        (lock_dir / "owner").write_text(
            f"pid={owner_pid}\ncreated_epoch=1\ntoken=live-owner\n",
            encoding="utf-8",
        )
        os.utime(lock_dir, (1, 1))

        _run_watchdog(env)

        assert lock_dir.exists()
        assert not (Path(env["HL_MEM_ROOT"]) / "var" / "watchdog.state").exists()
        watchdog_log = Path(env["HL_MEM_ROOT"]) / "var" / "watchdog.log"
        assert "watchdog_skipped reason=lock_owner_alive" in watchdog_log.read_text(encoding="utf-8")
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_watchdog_does_not_restart_when_taskkill_fails(
    watchdog_environment: tuple[dict[str, str], Path],
) -> None:
    env, actions = watchdog_environment
    _run_watchdog(env)
    _run_watchdog(env)
    env["HL_MEM_TEST_TASKKILL_EXIT"] = "1"

    result = _invoke_watchdog(env)

    assert result.returncode == 1
    assert _state(env["HL_MEM_ROOT"]) == {"failures": 3, "last_restart_epoch": 0}
    assert "restart " not in actions.read_text(encoding="utf-8")
    watchdog_log = Path(env["HL_MEM_ROOT"]) / "var" / "watchdog.log"
    assert "restart_aborted reason=termination_failed" in watchdog_log.read_text(encoding="utf-8")


def test_watchdog_does_not_mark_restart_when_launcher_exits(
    watchdog_environment: tuple[dict[str, str], Path],
) -> None:
    env, _ = watchdog_environment
    _run_watchdog(env)
    _run_watchdog(env)
    env["HL_MEM_TEST_START_EXIT"] = "1"

    result = _invoke_watchdog(env)

    assert result.returncode == 1
    assert _state(env["HL_MEM_ROOT"]) == {"failures": 3, "last_restart_epoch": 0}
    watchdog_log = Path(env["HL_MEM_ROOT"]) / "var" / "watchdog.log"
    assert "restart_aborted reason=launcher_exited" in watchdog_log.read_text(encoding="utf-8")


def test_watchdog_bounds_slow_diagnostic_commands(
    watchdog_environment: tuple[dict[str, str], Path],
) -> None:
    env, _ = watchdog_environment
    env["HL_MEM_WATCHDOG_DIAGNOSTIC_TIMEOUT_SECONDS"] = "1"
    env["HL_MEM_TEST_POWERSHELL_SLEEP"] = "5"
    _run_watchdog(env)
    _run_watchdog(env)

    started_at = time.monotonic()
    _run_watchdog(env)
    elapsed = time.monotonic() - started_at

    assert elapsed < 8
    assert _state(env["HL_MEM_ROOT"])["failures"] == 0
