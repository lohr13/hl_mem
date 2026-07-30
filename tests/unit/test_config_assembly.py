"""统一 Settings 快照装配测试。"""

from __future__ import annotations

import json
from pathlib import Path

from hl_mem import components
from hl_mem.adapters.hermes.provider import HLMemProvider
from hl_mem.api.server import create_app
from hl_mem.domain.entity import normalize_entity_id
from hl_mem.mcp.server import McpMemoryServer
from hl_mem.settings import Settings
from hl_mem.workers.worker import Worker


def test_api_worker_mcp_and_provider_reuse_same_settings(tmp_path: Path) -> None:
    settings = Settings(database_path=str(tmp_path / "shared.db"))

    app = create_app(settings)
    worker = Worker(settings)
    mcp = McpMemoryServer(settings)
    provider = HLMemProvider(settings=settings)
    try:
        assert app.state.settings is settings
        assert worker.settings is settings
        assert mcp.settings is settings
        assert provider.settings is settings
    finally:
        worker.database.close()
        mcp.database.close()
        app.state.db.close()


def test_initialize_process_loads_aliases_from_settings(tmp_path: Path) -> None:
    aliases_path = tmp_path / "aliases.json"
    aliases_path.write_text(
        json.dumps({"project": "hl_mem"}, ensure_ascii=False),
        encoding="utf-8",
    )

    components.initialize_process(Settings(entity_aliases_path=str(aliases_path)))
    try:
        assert normalize_entity_id("PROJECT") == "hl_mem"
    finally:
        components.initialize_process(Settings())
