from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
import venv
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "provider_plugin"


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)


@pytest.mark.timeout(180)
def test_external_provider_wheel_resolves_and_conflict_fails_before_server_construction(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required for the clean-wheel integration")
    distributions = tmp_path / "dist"
    distributions.mkdir()
    core_build = _run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(distributions)],
        cwd=ROOT,
    )
    assert core_build.returncode == 0, core_build.stdout + core_build.stderr
    plugin_build = _run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(distributions)],
        cwd=FIXTURE,
    )
    assert plugin_build.returncode == 0, plugin_build.stdout + plugin_build.stderr

    environment = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True, clear=True).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    wheels = sorted(distributions.glob("*.whl"))
    install = _run([uv, "pip", "install", "--python", str(python), *map(str, wheels)], cwd=tmp_path)
    assert install.returncode == 0, install.stdout + install.stderr

    probe = tmp_path / "probe.py"
    probe.write_text(
        textwrap.dedent("""
            from dataclasses import replace
            from pathlib import Path
            from tempfile import TemporaryDirectory

            from hl_mem.api.server import create_app
            from hl_mem.errors import PluginConflictError
            from hl_mem.plugins.registry import build_provider_registry
            from hl_mem.settings import Settings

            with TemporaryDirectory() as temporary:
                base = replace(
                    Settings.for_test(),
                    database_path=str(Path(temporary) / "memory.db"),
                    llm_provider="fixture_llm",
                    plugins_enabled=("fixture.provider",),
                )
                registry = build_provider_registry(base)
                adapter = registry.create_llm("fixture_llm", {})
                assert adapter.capabilities.json_object

                conflict = replace(
                    Settings.for_test(),
                    database_path=str(Path(temporary) / "conflict.db"),
                    plugins_enabled=("fixture.conflict",),
                )
                try:
                    create_app(conflict)
                except PluginConflictError:
                    pass
                else:
                    raise AssertionError("plugin conflict did not stop server construction")
            print("external-wheel-ok")
            """),
        encoding="utf-8",
    )
    process_environment = os.environ.copy()
    process_environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [str(python), str(probe)],
        cwd=tmp_path,
        env=process_environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "external-wheel-ok"
