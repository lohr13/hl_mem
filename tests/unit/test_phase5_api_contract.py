from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import hl_mem.api.server as server_module
from hl_mem.api.server import create_app
from hl_mem.settings import Settings


def test_domain_routes_are_registered_without_changing_public_paths(tmp_path: Path) -> None:
    app = create_app(replace(Settings.for_test(), database_path=str(tmp_path / "routes.db")))
    endpoints = {route.path: route.endpoint.__module__ for route in app.routes if hasattr(route, "endpoint")}

    assert endpoints["/v1/events"] == "hl_mem.api.routes.memory"
    assert endpoints["/v1/memories"] == "hl_mem.api.routes.memory"
    assert endpoints["/v1/recall"] == "hl_mem.api.routes.recall"
    assert endpoints["/v1/episodes"] == "hl_mem.api.routes.experience"
    assert endpoints["/v1/consolidate"] == "hl_mem.api.routes.maintenance"
    assert endpoints["/v1/stats"] == "hl_mem.api.routes.maintenance"
    assert endpoints["/healthz"] == "hl_mem.api.server"


def test_application_factory_keeps_runtime_patch_points() -> None:
    assert server_module.RecallService.__name__ == "RecallService"
    assert callable(server_module.components.make_extractor)
