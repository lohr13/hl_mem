from __future__ import annotations

import importlib

import pytest

RELEASE_MODULES = (
    "tests.integration.test_provider_plugin_wheel",
    "tests.release.test_migration_release_gate",
    "tests.test_migration_upgrade",
)


def _mark_names(module_name: str) -> set[str]:
    value = getattr(importlib.import_module(module_name), "pytestmark", ())
    marks = value if isinstance(value, (list, tuple)) else (value,)
    return {mark.mark.name for mark in marks}


@pytest.mark.parametrize("module_name", RELEASE_MODULES)
def test_release_modules_are_explicitly_marked(module_name: str) -> None:
    assert "release_only" in _mark_names(module_name)
