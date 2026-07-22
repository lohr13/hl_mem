"""hl_mem server + worker launcher."""
import os, threading
from pathlib import Path

# Load .env
env_file = Path(__file__).parent / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

os.environ.setdefault("HL_MEM_RERANKER", "on")
os.environ.setdefault("HL_MEM_EMBEDDER", "real")
os.environ.setdefault("HL_MEM_EXTRACTOR", "llm")

from hl_mem.storage.database import default_database_path

db_path = str(default_database_path())

# Start Worker in background
from hl_mem.workers.worker import Worker
from hl_mem.observability.audit import AuditLogger

audit = AuditLogger(db_path, enabled=True)
worker = Worker(db_path, {"audit": audit})
threading.Thread(target=worker.run_forever, daemon=True).start()
print("Worker started, db=" + db_path)

import uvicorn
uvicorn.run("hl_mem.api.server:app", host="127.0.0.1", port=8200)
