from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ISOLATED_KEYS = ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CONDA_PREFIX")


def _polluted_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in ISOLATED_KEYS:
        environment[key] = f"host-{key.casefold()}"
    return environment


def _environment_probe() -> str:
    return (
        "import json,os,sys;"
        "print(json.dumps({'environment':{key:os.environ.get(key) for key in "
        f"{ISOLATED_KEYS!r}"  # literal expectation, independent of launcher code
        "},'prefix':sys.prefix}))"
    )


def _last_json_line(output: str) -> dict[str, object]:
    return json.loads(output.strip().splitlines()[-1])


def test_windows_startup_delegates_to_isolated_repository_launcher() -> None:
    script = (ROOT / "start_production.bat").read_text(encoding="utf-8")
    launcher = (ROOT / "scripts" / "hlmem-python.cmd").read_text(encoding="utf-8")

    assert "%~dp0" in script
    assert "scripts\\hlmem-python.cmd" in script
    assert "hl_mem.toml" in script
    assert "start_server.py" in script
    assert "HL_MEM_" not in script
    assert "D:\\workspace" not in script
    assert "uvicorn" not in script
    assert "%~dp0.." in launcher
    assert ".venv\\Scripts\\python.exe" in launcher
    assert 'set "PYTHONPATH="' in launcher
    assert 'set "PYTHONHOME="' in launcher


def test_shell_startup_delegates_to_isolated_repository_launcher() -> None:
    script_path = ROOT / "start_hl_mem.sh"
    script = script_path.read_text(encoding="utf-8")
    launcher = (ROOT / "scripts" / "hlmem-python.sh").read_text(encoding="utf-8")

    assert b"\r\n" not in script_path.read_bytes()
    assert "BASH_SOURCE[0]" in script
    assert "scripts/hlmem-python.sh" in script
    assert "hl_mem.toml" in script
    assert "start_server.py" in script
    assert "HL_MEM_" not in script
    assert "/d/workspace" not in script
    assert not (ROOT / "start_v017.sh").exists()
    assert "BASH_SOURCE[0]" in launcher
    assert ".venv/Scripts/python.exe" in launcher
    assert "unset PYTHONPATH PYTHONHOME" in launcher


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher behavior")
def test_windows_repository_launcher_drops_all_host_python_environment() -> None:
    completed = subprocess.run(
        [str(ROOT / "scripts" / "hlmem-python.cmd"), "-c", _environment_probe()],
        cwd=ROOT,
        env=_polluted_environment(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = _last_json_line(completed.stdout)
    assert payload["environment"] == {key: None for key in ISOLATED_KEYS}
    assert Path(str(payload["prefix"])).resolve() == (ROOT / ".venv").resolve()


@pytest.mark.skipif(os.name != "nt", reason="Windows evaluation launcher behavior")
def test_windows_evaluation_launcher_rejects_host_python_path_before_imports() -> None:
    completed = subprocess.run(
        [str(ROOT / "scripts" / "hlmem-eval-python.cmd"), "-c", _environment_probe()],
        cwd=ROOT,
        env=_polluted_environment(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = _last_json_line(completed.stdout)
    assert payload["environment"] == {key: None for key in ISOLATED_KEYS}
    assert Path(str(payload["prefix"])).resolve() == (ROOT / ".venv").resolve()


def test_server_entrypoint_enables_hl_mem_info_logging_without_touching_uvicorn() -> None:
    bootstrap = textwrap.dedent("""
        import logging
        import runpy
        import sys
        import threading
        from pathlib import Path

        root = Path(sys.argv[1])
        sys.path.insert(0, str(root / "src"))

        import uvicorn
        from hl_mem import components
        from hl_mem.api import server
        from hl_mem import config_loader
        from hl_mem.observability import audit
        from hl_mem.workers import worker

        class FakeSettings:
            database_path = "unused.db"

        class FakeAuditLogger:
            def __init__(self, *args, **kwargs):
                pass

        class FakeWorker:
            def __init__(self, *args, **kwargs):
                pass

            def run_forever(self):
                pass

        class FakeThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                pass

        components.initialize_process = lambda settings: None
        server.create_app = lambda settings, audit=None: object()
        config_loader.load_settings = lambda: FakeSettings()
        audit.AuditLogger = FakeAuditLogger
        worker.Worker = FakeWorker
        threading.Thread = FakeThread

        uvicorn_handler = logging.NullHandler()
        uvicorn_logger = logging.getLogger("uvicorn")
        uvicorn_logger.handlers = [uvicorn_handler]
        uvicorn_logger.setLevel(logging.ERROR)
        uvicorn_logger.propagate = False

        def fake_uvicorn_run(app, *, host, port, workers, reload, log_config=None):
            assert uvicorn_handler._closed is False
            assert uvicorn_logger.handlers == [uvicorn_handler]
            assert uvicorn_logger.level == logging.ERROR
            assert uvicorn_logger.propagate is False
            assert workers == 1
            assert reload is False
            assert log_config is not None

            logging.config.dictConfig(log_config)
            application_logger = logging.getLogger("hl_mem.api.server")
            application_logger.debug("debug_noise")
            application_logger.info(
                "request_started method=GET path=/healthz"
            )
            logging.getLogger("unrelated").info("third_party_noise")

        uvicorn.run = fake_uvicorn_run
        runpy.run_path(str(root / "start_server.py"), run_name="__main__")
        """)

    result = subprocess.run(
        [sys.executable, "-c", bootstrap, str(ROOT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "INFO hl_mem.api.server request_started method=GET path=/healthz" in result.stderr
    assert "debug_noise" not in result.stderr
    assert "third_party_noise" not in result.stderr
