"""Tests for suite-wide environment defaults."""

from __future__ import annotations

import os
import runpy
from pathlib import Path

import pytest

CONFTEST_PATH = Path(__file__).parents[1] / "conftest.py"
SAFE_DEFAULTS = {
    "HL_MEM_ENV": "test",
    "HL_MEM_EXTRACTOR": "fake",
    "HL_MEM_EMBEDDER": "fake",
    "HL_MEM_RERANKER": "off",
}


def test_conftest_sets_safe_environment_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in SAFE_DEFAULTS:
        monkeypatch.delenv(name, raising=False)

    runpy.run_path(str(CONFTEST_PATH))

    assert {name: os.environ[name] for name in SAFE_DEFAULTS} == SAFE_DEFAULTS


@pytest.mark.parametrize("name", SAFE_DEFAULTS)
def test_conftest_preserves_explicit_environment(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    for environment_name in SAFE_DEFAULTS:
        monkeypatch.delenv(environment_name, raising=False)
    monkeypatch.setenv(name, "explicit-value")

    runpy.run_path(str(CONFTEST_PATH))

    assert os.environ[name] == "explicit-value"
