"""Behavior tests for the standalone cross-platform healthcheck."""

from __future__ import annotations

import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest

HEALTHCHECK = Path(__file__).resolve().parents[2] / "scripts" / "healthcheck.py"


@contextmanager
def _health_server(body: bytes, status: int = 200) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/healthz"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _run_healthcheck(url: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-S", str(HEALTHCHECK), "--url", url, "--timeout", "2"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_healthcheck_accepts_http_200_with_ok_status() -> None:
    with _health_server(b'{"status":"ok"}') as url:
        result = _run_healthcheck(url)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok: status=ok"
    assert len(result.stdout.splitlines()) == 1


@pytest.mark.parametrize(
    ("body", "status"),
    [(b'{"status":"degraded"}', 200), (b'{"status":"ok"}', 503), (b"not-json", 200)],
)
def test_healthcheck_rejects_unhealthy_responses(body: bytes, status: int) -> None:
    with _health_server(body, status) as url:
        result = _run_healthcheck(url)

    assert result.returncode == 1
    assert result.stdout.startswith("fail: ")
    assert len(result.stdout.splitlines()) == 1
