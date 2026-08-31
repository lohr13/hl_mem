"""Domain route registration extracted from the application factory."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query

from hl_mem.api.routes import resolve_namespace_alias, utc_now
from hl_mem.api.schemas import EpisodeInput, EpisodeUpdate, FeedbackInput, TraceInput
from hl_mem.application.correction import CorrectionService
from hl_mem.application.ingest import new_id
from hl_mem.experience.service import ExperienceService, InvalidStateTransitionError, backprop_episode_reward


def add_experience_routes(
    app: FastAPI,
    *,
    get_connection: Callable[..., Any],
    settings: Any,
    recall_side_effects: Any,
    embedder: Any,
) -> None:
    @app.post("/v1/episodes")
    def create_episode(
        payload: EpisodeInput, connection: sqlite3.Connection = Depends(get_connection)
    ) -> dict[str, Any]:
        episode_id = new_id()
        service = ExperienceService(connection)
        service.create_episode(
            episode_id,
            payload.goal,
            utc_now(),
            payload.session_id,
            payload.task_type,
            namespace=payload.effective_namespace,
        )
        return service.get_episode(episode_id)

    @app.post("/v1/feedback")
    def post_feedback(
        payload: FeedbackInput, connection: sqlite3.Connection = Depends(get_connection)
    ) -> dict[str, Any]:
        try:
            result: dict[str, Any] = ExperienceService(
                connection,
                settings=settings,
                pending_exposure_check=recall_side_effects.has_pending_exposures,
            ).submit_retrieval_feedback_eventually(
                payload.feedback_id, payload.helpful, payload.task_outcome, utc_now()
            )
        except ValueError as error:
            if str(error).startswith("feedback exposure not found:"):
                raise HTTPException(404, str(error)) from error
            raise
        correction = payload.correction
        if correction is None:
            return result
        correction_result = CorrectionService(connection, embedder, settings=settings).apply(
            correction.memory_id,
            action=correction.action,
            corrected_text=correction.corrected_text,
            idempotency_key=correction.idempotency_key,
        )
        result["correction"] = correction_result
        result["correction_event_id"] = correction_result["correction_event_id"]
        return result

    @app.post("/v1/episodes/{episode_id}/traces")
    def add_episode_trace(
        episode_id: str,
        payload: TraceInput,
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        service = ExperienceService(connection)
        try:
            trace_id = service.add_trace(
                episode_id,
                payload.action,
                payload.observation,
                payload.error_signature,
                payload.value,
            )
        except InvalidStateTransitionError as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(404, str(error)) from error
        return {"id": trace_id, "episode_id": episode_id}

    @app.patch("/v1/episodes/{episode_id}")
    def update_episode(
        episode_id: str,
        payload: EpisodeUpdate,
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        service = ExperienceService(connection)
        try:
            connection.execute("BEGIN IMMEDIATE")
            updated = service.update_episode(
                episode_id,
                utc_now(),
                payload.status,
                payload.reward,
                payload.outcome_summary,
                commit=False,
            )
            if payload.reward is not None:
                backprop_episode_reward(connection, episode_id, payload.reward, commit=False)
                updated = service.get_episode(episode_id)
            connection.commit()
            return updated
        except InvalidStateTransitionError as error:
            connection.rollback()
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            connection.rollback()
            raise HTTPException(404, str(error)) from error
        except Exception:
            connection.rollback()
            raise

    @app.get("/v1/episodes")
    def list_episodes(
        limit: int = 20,
        status: str | None = None,
        namespace: str | None = Query(default=None, min_length=1, max_length=100),
        tenant_id: str | None = Query(
            default=None,
            min_length=1,
            max_length=100,
            deprecated=True,
        ),
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise HTTPException(422, "limit must be between 1 and 100")
        effective_namespace = resolve_namespace_alias(namespace, tenant_id)
        return {
            "episodes": ExperienceService(connection).list_episodes(
                limit,
                status,
                namespace=effective_namespace,
            )
        }

    @app.get("/v1/episodes/{episode_id}")
    def get_episode(episode_id: str, connection: sqlite3.Connection = Depends(get_connection)) -> dict[str, Any]:
        try:
            return ExperienceService(connection).get_episode(episode_id)
        except ValueError as error:
            raise HTTPException(404, str(error)) from error

    @app.get("/v1/policies")
    def list_policies(
        status: str = "active",
        namespace: str | None = Query(default=None, min_length=1, max_length=100),
        tenant_id: str | None = Query(
            default=None,
            min_length=1,
            max_length=100,
            deprecated=True,
        ),
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        effective_namespace = resolve_namespace_alias(namespace, tenant_id)
        return {
            "policies": ExperienceService(connection).list_policies(
                status,
                namespace=effective_namespace,
            )
        }
