from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_windows_startup_uses_repository_relative_entrypoint() -> None:
    script = (ROOT / "start_production.bat").read_text(encoding="utf-8")

    assert "%~dp0" in script
    assert ".venv\\Scripts\\python.exe" in script
    assert "hl_mem.toml" in script
    assert "start_server.py" in script
    assert "HL_MEM_" not in script
    assert "D:\\workspace" not in script
    assert "uvicorn" not in script


def test_shell_startup_supports_posix_and_windows_virtualenvs() -> None:
    script_path = ROOT / "start_hl_mem.sh"
    script = script_path.read_text(encoding="utf-8")

    assert b"\r\n" not in script_path.read_bytes()
    assert "BASH_SOURCE[0]" in script
    assert ".venv/bin/python" in script
    assert ".venv/Scripts/python.exe" in script
    assert "hl_mem.toml" in script
    assert "start_server.py" in script
    assert "HL_MEM_" not in script
    assert "/d/workspace" not in script
    assert not (ROOT / "start_v017.sh").exists()


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

        def fake_uvicorn_run(app, *, host, port, log_config=None):
            assert uvicorn_handler._closed is False
            assert uvicorn_logger.handlers == [uvicorn_handler]
            assert uvicorn_logger.level == logging.ERROR
            assert uvicorn_logger.propagate is False
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
