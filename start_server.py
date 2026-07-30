"""hl_mem server + worker launcher."""

import threading

import uvicorn

from hl_mem.domain.entity import load_entity_aliases, set_active_aliases
from hl_mem.observability.audit import AuditLogger
from hl_mem.settings import Settings
from hl_mem.workers.worker import Worker

settings = Settings.from_env()
set_active_aliases(load_entity_aliases(settings.entity_aliases_path))
db_path = settings.database_path

audit = AuditLogger(db_path, enabled=True)
worker = Worker(settings, {"audit": audit})
threading.Thread(target=worker.run_forever, daemon=True).start()
print("Worker started, db=" + db_path)

uvicorn.run("hl_mem.api.server:app", host="127.0.0.1", port=8200)
