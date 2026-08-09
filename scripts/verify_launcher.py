#!/usr/bin/env python3
"""Verify the source launchers against a contaminated Python environment."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_ROOT = (REPO_ROOT / ".venv").resolve()
RUNNERS = (
    REPO_ROOT / "evaluation" / "tools" / "run_longmemeval_benchmark.py",
    REPO_ROOT / "evaluation" / "tools" / "run_memdaily_benchmark.py",
    REPO_ROOT / "evaluation" / "tools" / "run_perltqa_benchmark.py",
)
PROBE_CODE = (
    "import json, os, sys, numpy; "
    "print(json.dumps({"
    "'pythonpath': os.environ.get('PYTHONPATH'), "
    "'pythonhome': os.environ.get('PYTHONHOME'), "
    "'prefix': sys.prefix, "
    "'numpy_file': numpy.__file__, "
    "'argument': sys.argv[1], "
    "'cwd': os.getcwd()"
    "}))"
)


def _is_within(path: str | Path, directory: Path) -> bool:
    path_text = os.path.normcase(os.path.realpath(path))
    directory_text = os.path.normcase(os.path.realpath(directory))
    try:
        return os.path.commonpath((path_text, directory_text)) == directory_text
    except ValueError:
        return False


def _launcher_commands() -> list[tuple[str, list[str]]]:
    commands: list[tuple[str, list[str]]] = []
    bash = shutil.which("bash")
    if bash:
        commands.append(("bash", [bash, (REPO_ROOT / "scripts" / "hlmem-python.sh").as_posix()]))
    if os.name == "nt":
        command_processor = os.environ.get("COMSPEC", "cmd.exe")
        commands.append(
            (
                "cmd",
                [command_processor, "/d", "/c", str(REPO_ROOT / "scripts" / "hlmem-python.cmd")],
            )
        )
    return commands


def _run(
    launcher: Sequence[str],
    arguments: Sequence[str],
    *,
    cwd: Path,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*launcher, *arguments],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def main() -> int:
    failures = 0

    def report(name: str, passed: bool, detail: str = "") -> None:
        nonlocal failures
        status = "PASS" if passed else "FAIL"
        suffix = f" - {detail}" if detail else ""
        print(f"{status}: {name}{suffix}")
        failures += int(not passed)

    report(
        "验证脚本自身的 PYTHONPATH/PYTHONHOME 已清理",
        "PYTHONPATH" not in os.environ and "PYTHONHOME" not in os.environ,
    )
    report("验证脚本自身使用 hl_mem .venv", _is_within(sys.prefix, VENV_ROOT), sys.prefix)
    try:
        import numpy

        numpy_file = str(numpy.__file__)
        report("验证脚本自身从 hl_mem .venv 导入 numpy", _is_within(numpy_file, VENV_ROOT), numpy_file)
    except ImportError as exc:
        report("验证脚本自身从 hl_mem .venv 导入 numpy", False, str(exc))

    launchers = _launcher_commands()
    report("发现可验证的 launcher", bool(launchers))
    with tempfile.TemporaryDirectory(prefix="hlmem launcher verify ") as temporary:
        outside_directory = Path(temporary)
        fake_site_packages = outside_directory / "external site-packages"
        fake_numpy = fake_site_packages / "numpy"
        fake_numpy.mkdir(parents=True)
        (fake_numpy / "__init__.py").write_text(
            "raise RuntimeError('poisoned numpy imported')\n",
            encoding="utf-8",
        )
        fake_python_home = outside_directory / "external python home"
        fake_python_home.mkdir()
        contaminated_environment = os.environ.copy()
        contaminated_environment["PYTHONPATH"] = str(fake_site_packages)
        contaminated_environment["PYTHONHOME"] = str(fake_python_home)

        for launcher_name, launcher in launchers:
            probe = _run(
                launcher,
                ("-c", PROBE_CODE, "argument with spaces"),
                cwd=outside_directory,
                environment=contaminated_environment,
            )
            probe_detail = probe.stderr.strip() or f"exit={probe.returncode}"
            payload: dict[str, object] = {}
            if probe.returncode == 0:
                try:
                    payload = json.loads(probe.stdout.strip().splitlines()[-1])
                except (IndexError, json.JSONDecodeError) as exc:
                    probe_detail = f"invalid probe output: {exc}"

            report(
                f"[{launcher_name}] 从非仓库目录调用成功",
                probe.returncode == 0 and bool(payload),
                probe_detail if probe.returncode != 0 or not payload else "",
            )
            report(
                f"[{launcher_name}] 清理伪造的 PYTHONPATH/PYTHONHOME",
                bool(payload) and payload.get("pythonpath") is None and payload.get("pythonhome") is None,
            )
            report(
                f"[{launcher_name}] sys.prefix 指向 hl_mem .venv",
                _is_within(str(payload.get("prefix", "")), VENV_ROOT),
                str(payload.get("prefix", "")),
            )
            report(
                f"[{launcher_name}] numpy.__file__ 位于 hl_mem .venv",
                _is_within(str(payload.get("numpy_file", "")), VENV_ROOT),
                str(payload.get("numpy_file", "")),
            )
            report(
                f"[{launcher_name}] 带空格参数透传",
                payload.get("argument") == "argument with spaces",
            )
            report(
                f"[{launcher_name}] 工作目录切换到仓库根",
                bool(payload) and Path(str(payload.get("cwd", ""))).resolve() == REPO_ROOT,
                str(payload.get("cwd", "")),
            )

            nonzero = _run(
                launcher,
                ("-c", "import sys; sys.exit(23)"),
                cwd=outside_directory,
                environment=contaminated_environment,
            )
            report(
                f"[{launcher_name}] 非零退出码透传",
                nonzero.returncode == 23,
                f"exit={nonzero.returncode}",
            )

            for runner in RUNNERS:
                help_result = _run(
                    launcher,
                    (str(runner.relative_to(REPO_ROOT)), "--help"),
                    cwd=outside_directory,
                    environment=contaminated_environment,
                )
                report(
                    f"[{launcher_name}] {runner.name} --help 返回 0",
                    help_result.returncode == 0,
                    help_result.stderr.strip() or f"exit={help_result.returncode}",
                )

    print(f"SUMMARY: {'PASS' if failures == 0 else 'FAIL'} ({failures} failed)")
    return int(failures != 0)


if __name__ == "__main__":
    raise SystemExit(main())
