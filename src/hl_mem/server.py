"""HL-Mem API 与后台 Worker 的统一进程启动器。"""

from __future__ import annotations

import threading
from copy import deepcopy

import uvicorn
from uvicorn.config import LOGGING_CONFIG as UVICORN_LOGGING_CONFIG

from hl_mem import components
from hl_mem.api.server import create_app
from hl_mem.config_loader import load_settings
from hl_mem.observability.audit import AuditLogger
from hl_mem.settings import Settings
from hl_mem.workers.worker import Worker

LOGGING_CONFIG = deepcopy(UVICORN_LOGGING_CONFIG)
LOGGING_CONFIG["formatters"]["hl_mem"] = {
    "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
}
LOGGING_CONFIG["handlers"]["hl_mem"] = {
    "class": "logging.StreamHandler",
    "formatter": "hl_mem",
    "level": "INFO",
    "stream": "ext://sys.stderr",
}
LOGGING_CONFIG["loggers"]["hl_mem"] = {
    "handlers": ["hl_mem"],
    "level": "INFO",
    "propagate": False,
}


def run_server(settings: Settings, *, host: str = "127.0.0.1", port: int = 8200) -> None:
    """使用同一配置启动后台 Worker 和单进程 uvicorn。"""
    components.initialize_process(settings)
    audit = AuditLogger(settings.database_path, enabled=True)
    worker = Worker(settings, audit_logger=audit)
    threading.Thread(target=worker.run_forever, daemon=True).start()
    print(f"Worker started, db={settings.database_path}")
    uvicorn.run(
        create_app(settings, audit=audit),
        host=host,
        port=port,
        workers=1,
        reload=False,
        log_config=LOGGING_CONFIG,
    )


def main() -> None:
    """从当前目录配置启动默认本地服务。"""
    run_server(load_settings())
