from __future__ import annotations

import zipfile
from pathlib import Path

from scripts.check_wheel_contents import check_wheel


def _write_wheel(path: Path, members: list[str]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for member in members:
            archive.writestr(member, "")
    return path


def test_wheel_requires_stable_evaluation_runner(tmp_path: Path) -> None:
    wheel = _write_wheel(tmp_path / "hl_mem.whl", ["hl_mem/__init__.py"])

    assert check_wheel(wheel) == ["missing stable evaluation module: hl_mem/evaluation/runner.py"]


def test_wheel_rejects_repository_benchmarks(tmp_path: Path) -> None:
    wheel = _write_wheel(
        tmp_path / "hl_mem.whl",
        ["hl_mem/evaluation/runner.py", "benchmarks/archive/v030/example.py"],
    )

    assert check_wheel(wheel) == ["repository benchmark leaked into wheel: benchmarks/archive/v030/example.py"]


def test_v030_rejection_is_activated_by_release_gate(tmp_path: Path) -> None:
    wheel = _write_wheel(
        tmp_path / "hl_mem.whl",
        ["hl_mem/evaluation/runner.py", "hl_mem/evaluation/v030_corpus.py"],
    )

    assert check_wheel(wheel) == []
    assert check_wheel(wheel, reject_v030=True) == [
        "historical v0.30 evaluation leaked into wheel: hl_mem/evaluation/v030_corpus.py"
    ]
