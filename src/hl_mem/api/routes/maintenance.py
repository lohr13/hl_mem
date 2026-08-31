"""Domain route registration extracted from the application factory."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any, cast

from fastapi import Depends, FastAPI, HTTPException

from hl_mem.api.routes import utc_now
from hl_mem.api.schemas import ConsolidationScopeInput
from hl_mem.application.ingest import new_id
from hl_mem.storage.jobs import JobRepository
from hl_mem.workers.automation import semantic_job_enabled


def add_maintenance_routes(
    app: FastAPI,
    *,
    get_connection: Callable[..., Any],
    settings: Any,
    provider_runtime: Any,
) -> None:
    @app.post("/v1/consolidate")
    def consolidate(
        payload: ConsolidationScopeInput,
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, str]:
        """创建带显式作用域的冲突归并任务。"""
        if not semantic_job_enabled(settings, "consolidate_conflicts"):
            raise HTTPException(status_code=409, detail="semantic conflict consolidation is disabled")
        job_id = new_id()
        now = utc_now()
        JobRepository(connection).insert_job(
            {
                "id": job_id,
                "job_type": "consolidate_conflicts",
                "payload": payload.model_dump(),
                "created_at": now,
                "updated_at": now,
            }
        )
        return {"id": job_id}

    @app.get("/v1/stats")
    def stats(
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        usage = provider_runtime.governor.snapshot() if provider_runtime is not None else None
        return {
            "events": connection.execute("SELECT count(*) FROM events").fetchone()[0],
            "claims": connection.execute("SELECT count(*) FROM claims").fetchone()[0],
            "tokens_today": (0 if usage is None else int(cast(dict[str, Any], usage["settled"])["total_tokens"])),
            "jobs_pending": connection.execute("SELECT count(*) FROM jobs WHERE status='pending'").fetchone()[0],
        }

    @app.get("/v1/jobs")
    def jobs(
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        repository = JobRepository(connection)
        return {**repository.counts(), "jobs": repository.list_jobs()}
