"""hl_mem server + worker launcher."""

import threading
from copy import deepcopy

import uvicorn
from uvicorn.config import LOGGING_CONFIG as UVICORN_LOGGING_CONFIG

from hl_mem import components
from hl_mem.api.server import create_app
from hl_mem.config_loader import load_settings
from hl_mem.observability.audit import AuditLogger
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

settings = load_settings()
components.initialize_process(settings)
db_path = settings.database_path

audit = AuditLogger(db_path, enabled=True)
worker = Worker(settings, audit_logger=audit)
threading.Thread(target=worker.run_forever, daemon=True).start()
print("Worker started, db=" + db_path)

uvicorn.run(
    create_app(settings, audit=audit),
    host="127.0.0.1",
    port=8200,
    workers=1,
    reload=False,
    log_config=LOGGING_CONFIG,
)
