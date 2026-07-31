from __future__ import annotations

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
