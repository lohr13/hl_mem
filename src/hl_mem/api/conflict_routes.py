"""冲突管理 REST 路由注册。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query

from hl_mem.api.schemas import (
    ConflictCaseListOutput,
    ConflictDossierOutput,
    ConflictResolutionInput,
    ConflictResolutionOutput,
    ConflictReviewOutput,
    ErrorOutput,
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
        """返回 group-native case 的完整候选 revision 快照。"""

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
        response_model=ConflictResolutionOutput,
        responses={409: {"description": "Stale conflict revision or state conflict"}},
    )
    def resolve_group_conflict(
        case_id: str,
        payload: ConflictResolutionInput,
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        """仅在 expected_revision 仍匹配时执行候选选择或拒绝。"""

        return ResolutionService(connection).resolve_group(
            case_id,
            payload.action,
            candidate_key=payload.candidate_key,
            expected_revision=payload.expected_revision,
            rationale=payload.rationale,
            resolver=payload.resolver,
        )
