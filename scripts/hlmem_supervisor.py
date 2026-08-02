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


@dataclass(frozen=True)
class SupervisorConfig:
    """Filesystem and process settings for a single supervision cycle."""

    repo_root: Path
    state_file: Path
    log_file: Path
    lock_dir: Path
    start_script: Path
    bash_executable: Path
    url: str = DEFAULT_URL
    timeout: float = 5.0
    failure_threshold: int = 3
    cooldown_seconds: int = 60
    lock_stale_seconds: int = 300
    service_port: int = 8200
    command_timeout_seconds: float = 10.0
    termination_wait_seconds: float = 10.0
    termination_poll_seconds: float = 0.2
    start_grace_seconds: float = 1.0


@dataclass
class SupervisorState:
    failures: int = 0
    last_restart_epoch: int = 0


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
        failures = int(raw["failures"])
        last_restart_epoch = int(raw["last_restart_epoch"])
        if failures < 0 or last_restart_epoch < 0:
            raise ValueError("state values must be non-negative")
        return SupervisorState(failures, last_restart_epoch)
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
        self._restart = restart_fn or (lambda: restart_service(config, self._log))

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
        try:
            healthy, reason = self._probe(self.config.url, self.config.timeout)
        except Exception as exc:  # A scheduled pythonw task must record unexpected probe failures.
            healthy, reason = False, f"probe exception: {_short_reason(exc)}"

        if healthy:
            state.failures = 0
            _save_state(self.config.state_file, state)
            self._log(f"probe ok url={self.config.url} detail={_short_reason(reason)}")
            return 0

        state.failures += 1
        _save_state(self.config.state_file, state)
        self._log(
            f"probe failed url={self.config.url} failures={state.failures} "
            f"reason={_short_reason(reason)}"
        )
        if state.failures < self.config.failure_threshold:
            return 1

        now = int(self._clock())
        elapsed = max(0, now - state.last_restart_epoch)
        if state.last_restart_epoch and elapsed < self.config.cooldown_seconds:
            remaining = self.config.cooldown_seconds - elapsed
            self._log(
                f"restart suppressed reason=cooldown remaining_seconds={remaining} "
                f"failures={state.failures}"
            )
            return 1

        try:
            launcher_pid = self._restart()
        except Exception as exc:
            self._log(f"restart failed reason={_short_reason(exc)} failures={state.failures}")
            return 1

        state.failures = 0
        state.last_restart_epoch = int(self._clock())
        _save_state(self.config.state_file, state)
        self._log(f"restart triggered launcher_pid={launcher_pid}")
        return 1


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
                f"port {config.service_port} is still occupied by pids="
                f"{','.join(str(pid) for pid in listeners)}"
            )
        time.sleep(config.termination_poll_seconds)


def restart_service(config: SupervisorConfig, log: LogFn) -> int:
    """Kill listeners on the service port and launch HL-Mem without a window."""

    listener_pids = _query_listener_pids(config)
    for process_id in listener_pids:
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

    if config.start_script.is_file():
        bash_executable = config.bash_executable.expanduser().absolute()
        command = [
            str(bash_executable),
            "--noprofile",
            "--norc",
            str(config.start_script),
        ]
        launcher = f"start_script={config.start_script}"
    else:
        command = [
            str(virtual_environment / "Scripts" / "python.exe"),
            "start_server.py",
        ]
        launcher = "start_script=direct_python"

    process = subprocess.Popen(
        command,
        cwd=str(config.repo_root),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=CREATE_NO_WINDOW,
    )
    if config.start_grace_seconds > 0:
        time.sleep(config.start_grace_seconds)
    return_code = process.poll()
    if return_code is not None:
        raise RuntimeError(f"launcher exited immediately with code {return_code}")
    log(f"service launch started launcher_pid={process.pid} {launcher}")
    return process.pid


def _default_bash_executable() -> Path:
    configured = os.environ.get("HL_MEM_BASH")
    if configured:
        resolved = shutil.which(configured)
        return Path(resolved or configured).expanduser().absolute()

    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "bin" / "bash.exe",
    ]
    discovered = shutil.which("bash.exe")
    if discovered:
        candidates.append(Path(discovered))
    return next((path.absolute() for path in candidates if path.is_file()), candidates[0].absolute())


def build_config(url: str = DEFAULT_URL, timeout: float = 5.0) -> SupervisorConfig:
    repo_root = Path(os.environ.get("HL_MEM_ROOT", str(REPO_ROOT))).expanduser().absolute()
    var_dir = repo_root / "var"
    start_script = Path(
        os.environ.get("HL_MEM_START_SCRIPT", str(Path.home() / "bin" / "start_hlmem.sh"))
    ).expanduser()
    if not start_script.is_absolute():
        start_script = repo_root / start_script
    start_script = start_script.absolute()
    return SupervisorConfig(
        repo_root=repo_root,
        state_file=var_dir / "supervisor.state",
        log_file=var_dir / "supervisor.log",
        lock_dir=var_dir / "supervisor.lock",
        start_script=start_script,
        bash_executable=_default_bash_executable(),
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
