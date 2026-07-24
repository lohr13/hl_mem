"""真实 LLM 与 Embedding 的端到端测试。"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hl_mem.ingest.budget import TokenBudget
from hl_mem.ingest.embedder import Embedder
from hl_mem.ingest.event_filter import EventFilter
from hl_mem.ingest.llm_extractor import LLMExtractor
from hl_mem.storage.database import Database
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.events import EventRepository
from hl_mem.storage.jobs import JobRepository
from hl_mem.workers.worker import Worker


def _load_env(path: Path) -> None:
    """从项目环境文件加载未设置的变量。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


@pytest.mark.real_api
def test_real_llm_embedding_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """使用临时目录验证真实提取、向量化、写入与全文召回链路。"""
    project_root = Path(__file__).resolve().parent.parent
    _load_env(project_root / ".env")
    db_path = tmp_path / "hl_mem_test.db"
    budget_path = tmp_path / "hl_mem_budget_test.json"
    monkeypatch.setenv("HL_MEM_DB_PATH", str(db_path))
    monkeypatch.setenv("HL_MEM_EXTRACTOR", "llm")
    monkeypatch.setenv("HL_MEM_EMBEDDER", "real")

    required = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "EMBEDDING_API_KEY", "EMBEDDING_BASE_URL", "EMBEDDING_MODEL")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        pytest.skip(f"缺少真实 API 配置: {', '.join(missing)}")

    extractor = LLMExtractor(os.environ["LLM_API_KEY"], os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"])
    embedder = Embedder(
        os.environ["EMBEDDING_API_KEY"],
        os.environ["EMBEDDING_BASE_URL"],
        os.environ["EMBEDDING_MODEL"],
        int(os.environ.get("EMBEDDING_DIM", "2048")),
    )
    budget = TokenBudget(daily_limit=500_000, path=budget_path)
    events = [
        {"event_type": "message", "actor_type": "user", "content": {"text": "我们项目用PostgreSQL，主库在上海"}},
        {"event_type": "message", "actor_type": "user", "content": {"text": "我喜欢深色模式，浅色太刺眼了"}},
        {"event_type": "message", "actor_type": "user", "content": {"text": "服务器用的是Ubuntu 22.04"}},
        {"event_type": "message", "actor_type": "user", "content": {"text": "现在改用浅色模式了，深色看不清代码"}},
        {"event_type": "explicit_memory", "actor_type": "user", "content": {"text": "记住我的Git用户名是lohr13"}},
        {"event_type": "message", "actor_type": "user", "content": {"text": "好的，没问题"}},
    ]

    database = Database(db_path)
    connection = database.open()
    try:
        event_repo = EventRepository(connection)
        job_repo = JobRepository(connection)
        for index, event_data in enumerate(events):
            now = datetime.now(timezone.utc).isoformat()
            event_id = uuid.uuid4().hex
            content_json = json.dumps(event_data["content"], ensure_ascii=False, sort_keys=True)
            created = event_repo.insert_event(
                {
                    "id": event_id,
                    "idempotency_key": f"e2e-{index}",
                    "event_type": event_data["event_type"],
                    "actor_type": event_data["actor_type"],
                    "content_json": content_json,
                    "occurred_at": now,
                    "recorded_at": now,
                    "content_hash": hashlib.sha256(content_json.encode()).hexdigest(),
                    "sensitivity": "normal",
                }
            )
            if created:
                job_repo.insert_job(
                    {
                        "id": uuid.uuid4().hex,
                        "job_type": "extract_event",
                        "payload_json": json.dumps({"event_id": event_id}),
                        "idempotency_key": f"extract:{event_id}",
                        "created_at": now,
                        "updated_at": now,
                    }
                )
    finally:
        database.close()

    worker = Worker(
        db_path,
        {"extractor": extractor, "embedder": embedder, "budget": budget, "event_filter": EventFilter()},
    )
    try:
        for _ in range(len(events) + 3):
            if worker.run_once().get("status") == "idle":
                break
    finally:
        worker.database.close()

    verification_database = Database(db_path)
    connection = verification_database.open()
    try:
        claim_repo = ClaimRepository(connection)
        assert connection.execute("SELECT count(*) FROM claims").fetchone()[0] > 0
        assert claim_repo.search_claims_fts("PostgreSQL", 5)
        assert connection.execute("SELECT count(*) FROM jobs WHERE status='pending'").fetchone()[0] == 0
    finally:
        verification_database.close()
