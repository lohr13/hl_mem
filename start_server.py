"""hl_mem server + worker launcher."""

import threading

import uvicorn

from hl_mem import components
from hl_mem.api.server import create_app
from hl_mem.config_loader import load_settings
from hl_mem.observability.audit import AuditLogger
from hl_mem.workers.worker import Worker

settings = load_settings()
components.initialize_process(settings)
db_path = settings.database_path

audit = AuditLogger(db_path, enabled=True)
worker = Worker(settings, audit_logger=audit)
threading.Thread(target=worker.run_forever, daemon=True).start()
print("Worker started, db=" + db_path)

uvicorn.run(create_app(settings, audit=audit), host="127.0.0.1", port=8200)
