"""召回副作用的非阻塞投递边界。"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import queue
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Protocol

from hl_mem.observability.audit import audit_context
from hl_mem.observability.llm_spans import LLMSpanRecorder
from hl_mem.settings import Settings
from hl_mem.storage.database import Database
from hl_mem.storage.deferred_tasks import DeferredTaskRepository

LOGGER = logging.getLogger(__name__)


class RecallSideEffectSink(Protocol):
    """RecallService 所需的非阻塞副作用接口。"""

    def submit_access(self, query_id: str, claim_ids: list[str], accessed_at: str) -> bool: ...

    def submit_exposures(self, query_id: str, exposures: list[tuple[Any, ...]]) -> bool: ...

    def submit_resurrection(
        self,
        query_id: str,
        claim_id: str,
        embedding: bytes,
        embedding_model: str,
        embedding_dim: int,
        *,
        namespace: str,
        as_of: str,
        known_as_of: str | None,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class _DeferredSubmission:
    operation: str
    task_type: str
    resource_type: str
    resource_id: str
    payload: dict[str, Any]
    idempotency_key: str
    run_after: str
    max_attempts: int


@dataclass(frozen=True, slots=True)
class _AuditSubmission:
    target: Any
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    operation: str = "audit_emit"


@dataclass(frozen=True, slots=True)
class _LLMSpanSubmission:
    payload: dict[str, Any]
    operation: str = "audit_emit"


class DeferredAuditLogger:
    """保留 AuditLogger 契约但把 recall emit 移出请求线程。"""

    def __init__(self, target: Any, dispatcher: "RecallSideEffectDispatcher") -> None:
        self.target = target
        self.dispatcher = dispatcher
        self.enabled = bool(getattr(target, "enabled", False))

    def emit(self, *args: Any, **kwargs: Any) -> bool:
        if not self.enabled:
            return False
        captured = dict(audit_context.get())
        captured.update(kwargs)
        return self.dispatcher.submit_audit(self.target, args, captured)

    @contextmanager
    def span(self, phase: str, action: str, **dimensions: Any) -> Iterator[dict[str, Any]]:
        detail: dict[str, Any] = {}
        started = time.perf_counter_ns()
        try:
            yield detail
        except Exception as error:
            detail.update(error_class=type(error).__name__)
            self.emit(
                phase,
                action,
                "error",
                duration_us=(time.perf_counter_ns() - started) // 1000,
                detail=detail,
                **dimensions,
            )
            raise
        else:
            self.emit(
                phase,
                action,
                str(detail.pop("outcome", "success")),
                duration_us=(time.perf_counter_ns() - started) // 1000,
                detail=detail,
                **dimensions,
            )


class DeferredLLMSpanRecorder:
    """把 query-expansion span 交给 recall dispatcher 的写线程。"""

    def __init__(self, dispatcher: "RecallSideEffectDispatcher") -> None:
        self.dispatcher = dispatcher

    def record(self, **payload: Any) -> None:
        self.dispatcher.submit_llm_span(payload)


class RecallSideEffectDispatcher:
    """用有界内存队列隔离请求线程与 SQLite deferred task 写。"""

    def __init__(
        self,
        database: Database,
        *,
        settings: Settings | None = None,
        max_pending: int = 1024,
    ) -> None:
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        self.database = database
        self.settings = settings or database.settings
        self._queue: queue.Queue[_DeferredSubmission | _AuditSubmission | _LLMSpanSubmission | None] = queue.Queue(
            maxsize=max_pending
        )
        self._condition = threading.Condition()
        self._pending = 0
        self._closed = False
        self._accepted_exposure_ids: set[str] = set()
        self._health: dict[str, dict[str, int | str | None]] = {
            name: {"submitted": 0, "persisted": 0, "completed": 0, "failures": 0, "last_error": None}
            for name in ("access_record", "feedback_record", "audit_emit")
        }
        self._thread: threading.Thread | None = None

    def submit_access(self, query_id: str, claim_ids: list[str], accessed_at: str) -> bool:
        unique_ids = list(dict.fromkeys(str(claim_id) for claim_id in claim_ids if claim_id))
        if not unique_ids:
            return True
        return self._submit(
            _DeferredSubmission(
                operation="access_record",
                task_type="record_recall_access",
                resource_type="query",
                resource_id=query_id,
                payload={"claim_ids": unique_ids, "accessed_at": accessed_at},
                idempotency_key=f"record_recall_access:{query_id}",
                run_after=accessed_at,
                max_attempts=self.settings.recall_side_effect_max_attempts,
            )
        )

    def submit_exposures(self, query_id: str, exposures: list[tuple[Any, ...]]) -> bool:
        if not exposures:
            return True
        feedback_ids = [str(exposure[0]) for exposure in exposures]
        digest = hashlib.sha256(
            json.dumps(feedback_ids, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        updated_at = str(exposures[0][6])
        return self._submit(
            _DeferredSubmission(
                operation="feedback_record",
                task_type="record_recall_exposures",
                resource_type="query",
                resource_id=query_id,
                payload={"exposures": [list(exposure) for exposure in exposures]},
                idempotency_key=f"record_recall_exposures:{digest}",
                run_after=updated_at,
                max_attempts=self.settings.recall_side_effect_max_attempts,
            )
        )

    def submit_resurrection(
        self,
        query_id: str,
        claim_id: str,
        embedding: bytes,
        embedding_model: str,
        embedding_dim: int,
        *,
        namespace: str,
        as_of: str,
        known_as_of: str | None,
    ) -> bool:
        return self._submit(
            _DeferredSubmission(
                operation="access_record",
                task_type="resurrect_recalled_claim",
                resource_type="claim",
                resource_id=claim_id,
                payload={
                    "claim_id": claim_id,
                    "embedding_base64": base64.b64encode(embedding).decode("ascii"),
                    "embedding_model": embedding_model,
                    "embedding_dim": embedding_dim,
                    "namespace": namespace,
                    "as_of": as_of,
                    "known_as_of": known_as_of,
                },
                idempotency_key=f"resurrect_recalled_claim:{query_id}:{claim_id}",
                run_after=as_of,
                max_attempts=self.settings.recall_side_effect_max_attempts,
            )
        )

    def submit_audit(self, target: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
        return self._submit(_AuditSubmission(target, args, kwargs))

    def submit_llm_span(self, payload: dict[str, Any]) -> bool:
        return self._submit(_LLMSpanSubmission(dict(payload)))

    def _submit(self, submission: _DeferredSubmission | _AuditSubmission | _LLMSpanSubmission) -> bool:
        with self._condition:
            if self._closed:
                self._record_failure_locked(submission.operation, RuntimeError("dispatcher closed"))
                return False
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run,
                    name="hl-mem-recall-side-effects",
                    daemon=True,
                )
                self._thread.start()
            try:
                self._queue.put_nowait(submission)
            except queue.Full:
                self._record_failure_locked(submission.operation, RuntimeError("dispatcher queue full"))
                return False
            self._pending += 1
            if isinstance(submission, _DeferredSubmission) and submission.task_type == "record_recall_exposures":
                self._accepted_exposure_ids.update(
                    str(exposure[0])
                    for exposure in submission.payload.get("exposures", [])
                    if isinstance(exposure, list) and exposure
                )
            status = self._health[submission.operation]
            status["submitted"] = int(status["submitted"] or 0) + 1
            return True

    def has_pending_exposures(self, feedback_ids: list[str]) -> bool:
        """判断 receipt 是否仍在进程内等待 durable enqueue。"""
        with self._condition:
            return all(feedback_id in self._accepted_exposure_ids for feedback_id in feedback_ids)

    def health(self, connection: Any = None) -> dict[str, dict[str, int | str | None]]:
        """返回进程投递/持久化计数，并可合并 durable task 完成数。"""
        with self._condition:
            result = {name: dict(status) for name, status in self._health.items()}
        if connection is None:
            return result
        task_groups = {
            "access_record": ("record_recall_access", "resurrect_recalled_claim"),
            "feedback_record": (
                "record_recall_exposures",
                "apply_retrieval_feedback",
                "mark_recall_feedback_injected",
            ),
        }
        for operation, task_types in task_groups.items():
            placeholders = ",".join("?" for _ in task_types)
            row = connection.execute(
                f"SELECT COUNT(*),COALESCE(SUM(status='completed'),0) FROM deferred_tasks "
                f"WHERE task_type IN ({placeholders})",
                task_types,
            ).fetchone()
            result[operation]["completed"] = int(row[1])
        result["audit_emit"]["completed"] = result["audit_emit"]["persisted"]
        return result

    def _record_failure_locked(self, operation: str, error: Exception) -> None:
        status = self._health[operation]
        status["failures"] = int(status["failures"] or 0) + 1
        status["last_error"] = type(error).__name__

    def drain(self, timeout: float) -> bool:
        """在总 timeout 内等待当前已接纳任务写入 deferred_tasks。"""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def close(self, timeout: float) -> bool:
        """停止接纳新任务，尽力排空后结束单写线程。"""
        with self._condition:
            if self._closed:
                return self._thread is None or not self._thread.is_alive()
            self._closed = True
            thread = self._thread
        if thread is None:
            return True
        drained = self.drain(timeout)
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            return False
        thread.join(max(0.0, timeout))
        return drained and not thread.is_alive()

    @property
    def recommended_shutdown_timeout(self) -> float:
        """覆盖单项 durable enqueue 的 busy timeout 与退避预算。"""
        attempts = max(1, self.settings.recall_side_effect_max_attempts)
        backoff = self.settings.recall_side_effect_backoff_seconds * sum(2**attempt for attempt in range(attempts - 1))
        return float(max(5.0, attempts * self.database.busy_timeout_seconds + backoff + 1.0))

    def _run(self) -> None:
        while True:
            submission = self._queue.get()
            if submission is None:
                self._queue.task_done()
                return
            try:
                if isinstance(submission, _DeferredSubmission):
                    self._persist_deferred(submission)
                elif isinstance(submission, _AuditSubmission):
                    if submission.target.emit(*submission.args, **submission.kwargs) is False:
                        raise RuntimeError("audit emit rejected")
                else:
                    with self.database.connect() as connection:
                        LLMSpanRecorder(connection).record(**submission.payload)
                with self._condition:
                    status = self._health[submission.operation]
                    status["persisted"] = int(status["persisted"] or 0) + 1
            except Exception as error:
                if not isinstance(submission, _DeferredSubmission):
                    with self._condition:
                        self._record_failure_locked(submission.operation, error)
                LOGGER.exception("recall side-effect submission persistence failed: %s", submission.operation)
            finally:
                with self._condition:
                    if (
                        isinstance(submission, _DeferredSubmission)
                        and submission.task_type == "record_recall_exposures"
                    ):
                        self._accepted_exposure_ids.difference_update(
                            str(exposure[0])
                            for exposure in submission.payload.get("exposures", [])
                            if isinstance(exposure, list) and exposure
                        )
                    self._pending -= 1
                    self._condition.notify_all()
                self._queue.task_done()

    def _persist_deferred(self, submission: _DeferredSubmission) -> None:
        """在写线程内有界重试 durable enqueue，绝不反向阻塞 recall 请求。"""
        attempts = max(1, self.settings.recall_side_effect_max_attempts)
        for attempt in range(attempts):
            try:
                with self.database.connect() as connection:
                    DeferredTaskRepository(connection).defer(
                        task_type=submission.task_type,
                        resource_type=submission.resource_type,
                        resource_id=submission.resource_id,
                        payload=submission.payload,
                        idempotency_key=submission.idempotency_key,
                        run_after=submission.run_after,
                        max_attempts=submission.max_attempts,
                        error="",
                        updated_at=submission.run_after,
                    )
                return
            except Exception as error:
                with self._condition:
                    self._record_failure_locked(submission.operation, error)
                if attempt + 1 >= attempts:
                    raise
                delay = self.settings.recall_side_effect_backoff_seconds * (2**attempt)
                if delay > 0:
                    time.sleep(delay)
