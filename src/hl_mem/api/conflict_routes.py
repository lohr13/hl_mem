"""冲突管理 REST 路由注册。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query

from hl_mem.api.schemas import (
    ConflictCaseListOutput,
    ConflictDossierOutput,
    ConflictResolutionRequest,
    ConflictResolutionResult,
    ConflictReviewOutput,
    ErrorOutput,
    PairConflictResolutionInput,
)
from hl_mem.application.conflict_queries import ConflictDossierTooLargeError, ConflictQueryService
from hl_mem.application.conflicts import ResolutionService

ConnectionDependency = Callable[[], Iterator[sqlite3.Connection]]


def add_conflict_routes(
    app: FastAPI,
    *,
    get_connection: ConnectionDependency,
    get_read_connection: ConnectionDependency,
) -> None:
    """把冲突查询与裁决端点注册到应用。"""

    @app.get("/v1/conflicts", response_model=ConflictCaseListOutput)
    def list_open_conflicts(
        status: str | None = Query(default=None),
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        connection: sqlite3.Connection = Depends(get_read_connection),
    ) -> dict[str, Any]:
        """分页返回可供宿主 agent 轮询的未闭合冲突。"""

        statuses = status.split(",") if status is not None else None
        return ConflictQueryService(connection).list_open_cases(
            statuses=statuses,
            limit=limit,
            offset=offset,
        )

    @app.get("/v1/conflicts/{case_id}", response_model=ConflictReviewOutput)
    def review_conflict(
        case_id: str,
        connection: sqlite3.Connection = Depends(get_read_connection),
    ) -> dict[str, Any]:
        """返回 pair/group case 的 revision/fingerprint 快照。"""

        return ResolutionService(connection).review(case_id)

    @app.get(
        "/v1/conflicts/{case_id}/dossier",
        response_model=ConflictDossierOutput,
        responses={
            404: {"model": ErrorOutput, "description": "Conflict case not found"},
            413: {
                "model": ErrorOutput,
                "description": "Conflict dossier response exceeds the fixed size limit",
            },
        },
    )
    def conflict_dossier(
        case_id: str,
        connection: sqlite3.Connection = Depends(get_read_connection),
    ) -> dict[str, Any]:
        """返回 pair/group 共用的完整裁决案卷。"""

        try:
            return ConflictQueryService(connection).dossier(case_id)
        except ConflictDossierTooLargeError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc

    @app.post(
        "/v1/conflicts/{case_id}/resolve",
        response_model=ConflictResolutionResult,
        responses={409: {"description": "Stale conflict revision or state conflict"}},
    )
    def resolve_conflict(
        case_id: str,
        payload: ConflictResolutionRequest,
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        """按 action 词表判别 pair/group，并执行 revision/fingerprint CAS。"""

        service = ResolutionService(connection)
        if isinstance(payload, PairConflictResolutionInput):
            return service.resolve_pair(
                case_id,
                payload.action,
                expected_revision=payload.expected_revision,
                expected_fingerprint=payload.expected_fingerprint,
                rationale=payload.rationale,
                resolver=payload.resolver,
            )
        return service.resolve_group(
            case_id,
            payload.action,
            candidate_key=payload.candidate_key,
            expected_revision=payload.expected_revision,
            expected_fingerprint=payload.expected_fingerprint,
            rationale=payload.rationale,
            resolver=payload.resolver,
            confirm_retraction=payload.confirm_retraction,
        )
