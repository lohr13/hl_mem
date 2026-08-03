#!/usr/bin/env python3
"""Run one silent Windows supervision cycle for the HL-Mem service."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Literal

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from healthcheck import DEFAULT_URL  # noqa: E402
from healthcheck import probe as health_probe  # noqa: E402

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
LOCK_OWNER_FILENAME = "owner.json"

ProbeFn = Callable[[str, float], tuple[bool, str]]
RestartFn = Callable[[], int]
ClockFn = Callable[[], float]
LogFn = Callable[[str], None]
LockResult = Literal["acquired", "reclaimed", "busy"]
SupervisorStatus = Literal["idle", "starting"]


@dataclass(frozen=True)
class SupervisorConfig:
    """Filesystem and process settings for a single supervision cycle."""

    repo_root: Path
    state_file: Path
    log_file: Path
    lock_dir: Path
    url: str = DEFAULT_URL
    timeout: float = 5.0
    failure_threshold: int = 3
    cooldown_seconds: int = 60
    lock_stale_seconds: int = 300
    service_port: int = 8200
    command_timeout_seconds: float = 10.0
    termination_wait_seconds: float = 10.0
    termination_poll_seconds: float = 0.2
    starting_timeout_seconds: float = 60.0
    start_health_timeout_seconds: float = 30.0
    start_health_poll_seconds: float = 0.5
    server_log_max_bytes: int = 5 * 1024 * 1024
    server_log_backups: int = 3


@dataclass
class SupervisorState:
    failures: int = 0
    last_restart_epoch: int = 0
    status: SupervisorStatus = "idle"
    launcher_pid: int = 0
    starting_epoch: int = 0
    launcher_created_epoch: int = 0
    service_pid: int = 0
    service_created_epoch: int = 0


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    created_epoch: int
    executable_path: str
    command_line: str


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def append_log(path: Path, message: str) -> None:
    """Append one timestamped, single-line message to the supervisor log."""

    path.parent.mkdir(parents=True, exist_ok=True)
    clean_message = " ".join(str(message).splitlines())
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"{_timestamp()} {clean_message}\n")


def _load_state(path: Path, log: LogFn) -> SupervisorState:
    if not path.exists():
        return SupervisorState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise TypeError("state must be a JSON object")
        state = SupervisorState(
            failures=int(raw.get("failures", 0)),
            last_restart_epoch=int(raw.get("last_restart_epoch", 0)),
            status=str(raw.get("status", "idle")),
            launcher_pid=int(raw.get("launcher_pid", 0)),
            starting_epoch=int(raw.get("starting_epoch", 0)),
            launcher_created_epoch=int(raw.get("launcher_created_epoch", 0)),
            service_pid=int(raw.get("service_pid", 0)),
            service_created_epoch=int(raw.get("service_created_epoch", 0)),
        )
        numeric_values = (
            state.failures,
            state.last_restart_epoch,
            state.launcher_pid,
            state.starting_epoch,
            state.launcher_created_epoch,
            state.service_pid,
            state.service_created_epoch,
        )
        if any(value < 0 for value in numeric_values):
            raise ValueError("state values must be non-negative")
        if state.status not in ("idle", "starting"):
            raise ValueError(f"invalid supervisor status: {state.status}")
        if state.status == "starting" and (state.launcher_pid <= 0 or state.starting_epoch <= 0):
            raise ValueError("starting state requires launcher_pid and starting_epoch")
        return state
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError) as exc:
        log(f"state reset reason={_short_reason(exc)}")
        return SupervisorState()


def _save_state(path: Path, state: SupervisorState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(asdict(state), separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _short_reason(value: object, limit: int = 500) -> str:
    reason = " ".join(str(value).splitlines()) or "unknown"
    return reason[:limit]


def _pid_is_alive(process_id: int) -> bool:
    if process_id <= 0:
        return False
    if process_id == os.getpid():
        return True
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        ctypes.set_last_error(0)
        handle = kernel32.OpenProcess(process_query_limited_information, False, process_id)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return ctypes.get_last_error() == 5  # Access denied still proves the process exists.

    try:
        os.kill(process_id, 0)
    except OSError:
        return False
    return True


def _query_process_info(config: SupervisorConfig, process_id: int) -> ProcessInfo:
    if process_id <= 0:
        raise ValueError(f"invalid process id: {process_id}")
    powershell_script = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        f"$process = Get-CimInstance Win32_Process -Filter 'ProcessId = {process_id}' -ErrorAction Stop; "
        "if ($null -eq $process) { exit 3 }; "
        "$created = [DateTimeOffset]$process.CreationDate; "
        "[pscustomobject]@{"
        "pid=[int]$process.ProcessId;"
        "created_epoch=[long]$created.ToUnixTimeMilliseconds();"
        "executable_path=[string]$process.ExecutablePath;"
        "command_line=[string]$process.CommandLine"
        "} | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", powershell_script],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=CREATE_NO_WINDOW,
        timeout=config.command_timeout_seconds,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"process query failed for pid={process_id} exit={result.returncode}: " f"{_short_reason(result.stderr)}"
        )
    try:
        raw = json.loads(result.stdout)
        info = ProcessInfo(
            pid=int(raw["pid"]),
            created_epoch=int(raw["created_epoch"]),
            executable_path=str(raw.get("executable_path") or ""),
            command_line=str(raw.get("command_line") or ""),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid process query result for pid={process_id}: {_short_reason(exc)}") from exc
    if info.pid != process_id or info.created_epoch <= 0:
        raise RuntimeError(f"invalid process identity for pid={process_id}")
    return info


def _command_line_args(command_line: str) -> list[str]:
    if not command_line:
        return []
    if os.name != "nt":
        import shlex

        return shlex.split(command_line)

    import ctypes
    from ctypes import wintypes

    argument_count = ctypes.c_int()
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    ctypes.set_last_error(0)
    arguments = shell32.CommandLineToArgvW(command_line, ctypes.byref(argument_count))
    if not arguments:
        raise OSError(ctypes.get_last_error(), "CommandLineToArgvW failed")
    try:
        return [arguments[index] for index in range(argument_count.value)]
    finally:
        kernel32.LocalFree(ctypes.cast(arguments, wintypes.HLOCAL))


def _normalized_windows_path(path: str | Path) -> str:
    return str(path).replace("\\", "/").casefold().rstrip("/")


def _process_belongs_to_hl_mem(config: SupervisorConfig, info: ProcessInfo) -> bool:
    expected_executable = _normalized_windows_path(config.repo_root / ".venv" / "Scripts" / "python.exe")
    if _normalized_windows_path(info.executable_path) != expected_executable:
        return False
    try:
        arguments = _command_line_args(info.command_line)
    except (OSError, ValueError):
        return False
    if len(arguments) < 2:
        return False
    script_argument = _normalized_windows_path(arguments[1])
    expected_script = _normalized_windows_path(config.repo_root / "start_server.py")
    return script_argument == "start_server.py" or script_argument == expected_script


def _state_matches_process(state: SupervisorState, info: ProcessInfo) -> bool:
    service_matches = state.service_pid == info.pid and state.service_created_epoch == info.created_epoch
    launcher_matches = (
        state.status == "starting"
        and state.launcher_pid == info.pid
        and state.launcher_created_epoch == info.created_epoch
    )
    return service_matches or launcher_matches


def _clear_starting(state: SupervisorState) -> None:
    state.status = "idle"
    state.launcher_pid = 0
    state.starting_epoch = 0
    state.launcher_created_epoch = 0


class _ReclaimGuard:
    """OS-backed file lock that serializes stale-lock reclamation."""

    def __init__(self, lock_path: Path) -> None:
        self.path = lock_path.with_name(f".{lock_path.name}.reclaim")
        self._stream: BinaryIO | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        stream = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            if os.fstat(descriptor).st_size == 0:
                stream.write(b"\0")
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return False
        self._stream = stream
        return True

    def release(self) -> None:
        if self._stream is None:
            return
        descriptor = self._stream.fileno()
        try:
            self._stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            self._stream.close()
            self._stream = None


class _DirectoryLock:
    """Atomic directory lock that releases only its own ownership token."""

    def __init__(self, path: Path, now: float, stale_seconds: int, log: LogFn) -> None:
        self.path = path
        self.now = now
        self.stale_seconds = stale_seconds
        self.log = log
        self.token = uuid.uuid4().hex
        self.acquired = False

    @property
    def owner_file(self) -> Path:
        return self.path / LOCK_OWNER_FILENAME

    def acquire(self) -> LockResult:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.mkdir()
        except FileExistsError:
            return self._reclaim_if_stale()
        self._claim()
        return "acquired"

    def _claim(self) -> None:
        try:
            self.owner_file.write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "created_epoch": int(self.now),
                        "token": self.token,
                    },
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
        except Exception:
            try:
                self.owner_file.unlink(missing_ok=True)
                self.path.rmdir()
            except OSError:
                pass
            raise
        self.acquired = True

    def _read_owner(self) -> dict[str, object]:
        try:
            owner = json.loads(self.owner_file.read_text(encoding="utf-8"))
            return owner if isinstance(owner, dict) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def _reclaim_if_stale(self) -> LockResult:
        guard = _ReclaimGuard(self.path)
        if not guard.acquire():
            return "busy"
        try:
            try:
                age = max(0.0, self.now - self.path.stat().st_mtime)
            except OSError:
                return "busy"
            if age < self.stale_seconds:
                return "busy"

            owner = self._read_owner()
            try:
                owner_pid = int(owner.get("pid", 0))
            except (TypeError, ValueError):
                owner_pid = 0
            if _pid_is_alive(owner_pid):
                return "busy"

            quarantine = self.path.with_name(f".{self.path.name}.stale.{uuid.uuid4().hex}")
            try:
                self.path.replace(quarantine)
            except OSError:
                return "busy"

            try:
                try:
                    self.path.mkdir()
                except FileExistsError:
                    return "busy"
                self._claim()
                return "reclaimed"
            finally:
                shutil.rmtree(quarantine, ignore_errors=True)
        finally:
            guard.release()

    def release(self) -> None:
        if not self.acquired:
            return
        owner = self._read_owner()
        if owner.get("token") != self.token:
            self.log("lock release skipped reason=ownership changed")
            return
        try:
            self.owner_file.unlink()
            self.path.rmdir()
        except FileNotFoundError:
            return
        except OSError as exc:
            self.log(f"lock release failed reason={_short_reason(exc)}")


class Supervisor:
    """Coordinate persisted failure handling for one scheduled invocation."""

    def __init__(
        self,
        config: SupervisorConfig,
        *,
        probe_fn: ProbeFn = health_probe,
        restart_fn: RestartFn | None = None,
        clock: ClockFn = time.time,
        log_fn: LogFn | None = None,
    ) -> None:
        self.config = config
        self._probe = probe_fn
        self._clock = clock
        self._log = log_fn or (lambda message: append_log(config.log_file, message))
        self._restart = restart_fn

    def run_once(self) -> int:
        """Probe once, persist state, and restart only when policy permits."""

        lock = _DirectoryLock(
            self.config.lock_dir,
            self._clock(),
            self.config.lock_stale_seconds,
            self._log,
        )
        lock_result = lock.acquire()
        if lock_result == "busy":
            self._log("supervisor skipped reason=already running")
            return 0
        if lock_result == "reclaimed":
            self._log("stale lock reclaimed")

        try:
            return self._run_locked()
        finally:
            lock.release()

    def _run_locked(self) -> int:
        state = _load_state(self.config.state_file, self._log)
        healthy, reason = self._probe_once()

        if healthy:
            self._mark_healthy(state)
            self._log(f"probe ok url={self.config.url} detail={_short_reason(reason)}")
            return 0

        now = int(self._clock())
        if state.status == "starting":
            starting_age = max(0, now - state.starting_epoch)
            if starting_age < self.config.starting_timeout_seconds:
                self._log(
                    f"startup still in progress launcher_pid={state.launcher_pid} "
                    f"age_seconds={starting_age} process_alive={_pid_is_alive(state.launcher_pid)} "
                    f"healthz={_short_reason(reason)}"
                )
                return 1
            self._log(
                f"startup state timed out launcher_pid={state.launcher_pid} "
                f"age_seconds={starting_age} process_alive={_pid_is_alive(state.launcher_pid)} "
                f"healthz={_short_reason(reason)}"
            )
            _clear_starting(state)
            _save_state(self.config.state_file, state)

        try:
            listener_pids = _query_listener_pids(self.config)
        except Exception as exc:
            self._log(
                f"probe failed url={self.config.url} failures={state.failures} "
                f"reason={_short_reason(reason)} listener_query={_short_reason(exc)}"
            )
            return 1

        state.failures += 1
        _save_state(self.config.state_file, state)
        self._log(f"probe failed url={self.config.url} failures={state.failures} " f"reason={_short_reason(reason)}")

        if not listener_pids:
            self._log(f"port empty port={self.config.service_port}; starting immediately")
            return self._start_and_confirm(state)

        if state.failures < self.config.failure_threshold:
            return 1

        elapsed = max(0, now - state.last_restart_epoch)
        if state.last_restart_epoch and elapsed < self.config.cooldown_seconds:
            remaining = self.config.cooldown_seconds - elapsed
            self._log(f"restart suppressed reason=cooldown remaining_seconds={remaining} " f"failures={state.failures}")
            return 1

        return self._start_and_confirm(state)

    def _probe_once(self, timeout: float | None = None) -> tuple[bool, str]:
        try:
            probe_timeout = self.config.timeout if timeout is None else timeout
            return self._probe(self.config.url, probe_timeout)
        except Exception as exc:  # A scheduled pythonw task must record unexpected probe failures.
            return False, f"probe exception: {_short_reason(exc)}"

    def _record_healthy_identity(self, state: SupervisorState) -> None:
        try:
            listener_pids = _query_listener_pids(self.config)
        except Exception as exc:
            self._log(f"healthy listener identity not recorded reason={_short_reason(exc)}")
            return
        if len(listener_pids) != 1:
            self._log(
                f"healthy listener identity not recorded reason=expected one listener "
                f"pids={','.join(str(pid) for pid in listener_pids) or 'none'}"
            )
            return

        process_id = listener_pids[0]
        try:
            info = _query_process_info(self.config, process_id)
        except Exception as exc:
            self._log(f"healthy listener identity not recorded pid={process_id} " f"reason={_short_reason(exc)}")
            return
        if not _process_belongs_to_hl_mem(self.config, info):
            self._log(
                f"healthy listener identity not recorded pid={process_id} "
                f"reason=command line mismatch cmd={_short_reason(info.command_line)}"
            )
            return
        state.service_pid = info.pid
        state.service_created_epoch = info.created_epoch

    def _mark_healthy(self, state: SupervisorState) -> None:
        completed_start = state.status == "starting"
        self._record_healthy_identity(state)
        state.failures = 0
        if completed_start:
            state.last_restart_epoch = int(self._clock())
        _clear_starting(state)
        _save_state(self.config.state_file, state)

    def _start_and_confirm(self, state: SupervisorState) -> int:
        try:
            if self._restart is None:
                launcher_pid = restart_service(self.config, self._log, state)
            else:
                launcher_pid = self._restart()
            launcher_pid = int(launcher_pid)
            if launcher_pid <= 0:
                raise RuntimeError(f"invalid launcher pid: {launcher_pid}")
        except Exception as exc:
            self._log(f"restart failed reason={_short_reason(exc)} failures={state.failures}")
            return 1

        state.status = "starting"
        state.launcher_pid = launcher_pid
        state.starting_epoch = int(self._clock())
        state.launcher_created_epoch = 0
        state.service_pid = 0
        state.service_created_epoch = 0
        _save_state(self.config.state_file, state)
        self._log(f"restart triggered launcher_pid={launcher_pid} status=starting")
        deadline = time.monotonic() + max(0.0, self.config.start_health_timeout_seconds)
        try:
            launcher_info = _query_process_info(self.config, launcher_pid)
            if _process_belongs_to_hl_mem(self.config, launcher_info):
                state.launcher_created_epoch = launcher_info.created_epoch
                _save_state(self.config.state_file, state)
            else:
                self._log(
                    f"launcher identity command line mismatch pid={launcher_pid} "
                    f"cmd={_short_reason(launcher_info.command_line)}"
                )
        except Exception as exc:
            self._log(f"launcher identity not recorded pid={launcher_pid} reason={_short_reason(exc)}")

        last_reason = "not probed"
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._log(
                    f"startup confirmation timed out launcher_pid={launcher_pid} "
                    f"process_alive={_pid_is_alive(launcher_pid)} healthz={_short_reason(last_reason)} "
                    f"failures={state.failures}"
                )
                return 1
            probe_timeout = min(max(0.01, self.config.timeout), remaining)
            healthy, last_reason = self._probe_once(probe_timeout)
            if healthy:
                self._mark_healthy(state)
                self._log(f"startup confirmed launcher_pid={launcher_pid} " f"healthz={_short_reason(last_reason)}")
                return 1

            process_alive = _pid_is_alive(launcher_pid)
            remaining = deadline - time.monotonic()
            if not process_alive or remaining <= 0:
                outcome = "failed" if not process_alive else "timed out"
                self._log(
                    f"startup confirmation {outcome} launcher_pid={launcher_pid} "
                    f"process_alive={process_alive} healthz={_short_reason(last_reason)} "
                    f"failures={state.failures}"
                )
                return 1
            time.sleep(min(max(0.01, self.config.start_health_poll_seconds), remaining))


def _listener_pids(output: str, port: int) -> list[int]:
    listeners: set[int] = set()
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0].upper() != "TCP" or fields[3].upper() != "LISTENING":
            continue
        local_address = fields[1]
        port_text = local_address.rsplit(":", 1)[-1]
        if port_text == str(port) and fields[4].isdigit():
            listeners.add(int(fields[4]))
    return sorted(listeners)


def _query_listener_pids(config: SupervisorConfig) -> list[int]:
    netstat = subprocess.run(
        ["netstat.exe", "-ano", "-p", "tcp"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=CREATE_NO_WINDOW,
        timeout=config.command_timeout_seconds,
    )
    if netstat.returncode != 0:
        raise RuntimeError(f"netstat failed: {_short_reason(netstat.stderr)}")
    return _listener_pids(netstat.stdout, config.service_port)


def _wait_for_port_release(config: SupervisorConfig) -> None:
    deadline = time.monotonic() + config.termination_wait_seconds
    while True:
        listeners = _query_listener_pids(config)
        if not listeners:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"port {config.service_port} is still occupied by pids=" f"{','.join(str(pid) for pid in listeners)}"
            )
        time.sleep(config.termination_poll_seconds)


def _verify_listener_ownership(
    config: SupervisorConfig,
    state: SupervisorState,
    listener_pids: list[int],
    log: LogFn,
) -> None:
    for process_id in listener_pids:
        try:
            info = _query_process_info(config, process_id)
        except Exception as exc:
            log(f"端口冲突:拒绝终止 pid={process_id} cmd=<unavailable> " f"reason={_short_reason(exc)}")
            raise RuntimeError(f"port conflict: ownership unknown for pid={process_id}") from exc

        command_matches = _process_belongs_to_hl_mem(config, info)
        identity_matches = _state_matches_process(state, info)
        if not command_matches or not identity_matches:
            log(
                f"端口冲突:拒绝终止 pid={process_id} cmd={_short_reason(info.command_line)} "
                f"command_matches={command_matches} identity_matches={identity_matches}"
            )
            raise RuntimeError(f"port conflict: refusing to terminate pid={process_id}")


def _rotate_service_log(path: Path, max_bytes: int, backup_count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    retained_backups = max(0, backup_count)
    for candidate in path.parent.glob(f"{path.name}.*"):
        suffix = candidate.name.removeprefix(f"{path.name}.")
        if suffix.isdigit() and int(suffix) > retained_backups:
            candidate.unlink(missing_ok=True)

    if not path.exists() or path.stat().st_size < max(0, max_bytes):
        return
    if retained_backups == 0:
        path.unlink()
        return

    oldest = path.with_name(f"{path.name}.{retained_backups}")
    oldest.unlink(missing_ok=True)
    for index in range(retained_backups - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        if source.exists():
            source.replace(path.with_name(f"{path.name}.{index + 1}"))
    path.replace(path.with_name(f"{path.name}.1"))


def restart_service(
    config: SupervisorConfig,
    log: LogFn,
    state: SupervisorState | None = None,
) -> int:
    """Kill listeners on the service port and launch HL-Mem without a window."""

    listener_pids = _query_listener_pids(config)
    if listener_pids:
        effective_state = state or _load_state(config.state_file, log)
        _verify_listener_ownership(config, effective_state, listener_pids, log)
    for process_id in listener_pids:
        _verify_listener_ownership(config, effective_state, [process_id], log)
        result = subprocess.run(
            ["taskkill.exe", "/PID", str(process_id), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=CREATE_NO_WINDOW,
            timeout=config.command_timeout_seconds,
        )
        if result.returncode != 0:
            raise RuntimeError(f"taskkill failed for pid={process_id} exit={result.returncode}")
        log(f"process tree terminated pid={process_id}")
    if listener_pids:
        _wait_for_port_release(config)

    environment = os.environ.copy()
    virtual_environment = config.repo_root / ".venv"
    environment["VIRTUAL_ENV"] = str(virtual_environment)
    environment["PYTHONPATH"] = str(config.repo_root / "src")
    environment.pop("PYTHONHOME", None)

    command = [
        str(virtual_environment / "Scripts" / "python.exe"),
        "start_server.py",
    ]
    stdout_path = config.repo_root / "var" / "server.out.log"
    stderr_path = config.repo_root / "var" / "server.err.log"
    _rotate_service_log(stdout_path, config.server_log_max_bytes, config.server_log_backups)
    _rotate_service_log(stderr_path, config.server_log_max_bytes, config.server_log_backups)
    with (
        stdout_path.open("ab", buffering=0) as stdout_stream,
        stderr_path.open("ab", buffering=0) as stderr_stream,
    ):
        process = subprocess.Popen(
            command,
            cwd=str(config.repo_root),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout_stream,
            stderr=stderr_stream,
            close_fds=True,
            creationflags=CREATE_NO_WINDOW,
        )
    log(f"service launch started launcher_pid={process.pid} launcher=direct_python")
    return process.pid


def build_config(url: str = DEFAULT_URL, timeout: float = 5.0) -> SupervisorConfig:
    repo_root = Path(os.environ.get("HL_MEM_ROOT", str(REPO_ROOT))).expanduser().absolute()
    var_dir = repo_root / "var"
    return SupervisorConfig(
        repo_root=repo_root,
        state_file=var_dir / "supervisor.state",
        log_file=var_dir / "supervisor.log",
        lock_dir=var_dir / "supervisor.lock",
        url=url,
        timeout=timeout,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args(argv)
    config = build_config(args.url, args.timeout)
    try:
        return Supervisor(config).run_once()
    except Exception as exc:
        try:
            append_log(config.log_file, f"supervisor failed reason={_short_reason(exc)}")
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
