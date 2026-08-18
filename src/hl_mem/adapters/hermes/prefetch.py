"""Hermes receipt-free 结构化后台预取缓存。"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Literal

from hl_mem import __version__
from hl_mem.adapters.hermes.http_client import HLMemHttpClient
from hl_mem.application.context_packet import (
    RetrievalBundle,
    UnknownSchemaMajorError,
    retrieval_bundle_from_dict,
)
from hl_mem.http_utils import (
    HL_MEM_VERSION_HEADER,
    find_http_status_error,
    sanitize_http_response_body,
    validation_response_body,
)
from hl_mem.recall.injection import InjectionContext

logger = logging.getLogger(__name__)

PrefetchStatus = Literal["pending", "completed", "expired"]
MAX_RECALL_QUERY_LENGTH = 2_000
MAX_ON_DEMAND_RECALL_TIMEOUT_SECONDS = 8.0
PREFETCH_ERROR_THRESHOLD = 3


class PrefetchOverloadedError(RuntimeError):
    """Raised internally when the bounded prefetch admission queue is full."""


class PrefetchClosedError(RuntimeError):
    """Raised internally when work is queued after final cache shutdown."""


@dataclass(frozen=True, slots=True)
class PrefetchKey:
    """覆盖所有会改变 retrieval/packing 结果的稳定缓存键。"""

    session: str
    query_hash: str
    limit: int
    intent: str | None
    as_of: str | None
    known_as_of: str | None
    namespace: str
    token_budget: int
    projection_version: str
    delivery_purpose: str
    experiment_variant: str
    echo_variant: str
    freshness_variant: str
    policy_versions: tuple[tuple[str, str], ...]
    freshness_time_bucket: str


@dataclass(frozen=True, slots=True)
class PrefetchEntry:
    """显式记录一次 key 的 pending/completed/expired 生命周期。"""

    status: PrefetchStatus
    bundle: RetrievalBundle | None
    created_at: float
    expires_at: float | None
    error_type: str | None = None


class PrefetchCache:
    """按完整 key 独立预取并缓存 receipt-free RetrievalBundle。"""

    def __init__(
        self,
        client: HLMemHttpClient,
        ttl_seconds: float = 300.0,
        *,
        projection_version: str = "v1",
        clock: Callable[[], float] = time.monotonic,
        max_workers: int = 4,
        max_entries: int = 256,
        max_pending: int | None = None,
        on_demand_timeout_seconds: float = MAX_ON_DEMAND_RECALL_TIMEOUT_SECONDS,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if not projection_version:
            raise ValueError("projection_version must be non-empty")
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        if max_entries < 2:
            raise ValueError("max_entries must be at least two")
        resolved_max_pending = min(max_workers * 2, max_entries - 1) if max_pending is None else max_pending
        if resolved_max_pending < 1 or resolved_max_pending >= max_entries:
            raise ValueError("max_pending must be positive and smaller than max_entries")
        if on_demand_timeout_seconds <= 0:
            raise ValueError("on_demand_timeout_seconds must be positive")
        self.client = client
        self.ttl_seconds = ttl_seconds
        self.projection_version = projection_version
        self.max_entries = max_entries
        self.max_pending = resolved_max_pending
        self.on_demand_timeout_seconds = on_demand_timeout_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="hl-mem-prefetch",
        )
        self._futures: set[Future[None]] = set()
        self._future_keys: dict[Future[None], PrefetchKey] = {}
        self._values: dict[PrefetchKey, PrefetchEntry] = {}
        self._rejections: OrderedDict[PrefetchKey, PrefetchEntry] = OrderedDict()
        self._max_rejections = min(64, max_entries)
        self._closed = False
        self._retrieval_failures = 0
        self._schema_failures = 0
        self._overload_rejections = 0
        self._closed_rejections = 0
        self._consecutive_failures = 0
        self._last_error: str | None = None

    def queue(
        self,
        query: str,
        session_id: str,
        *,
        limit: int = 10,
        intent: str | None = None,
        as_of: str | None = None,
        known_as_of: str | None = None,
        namespace: str = "default",
        token_budget: int = 2000,
        projection_version: str | None = None,
        injection_context: InjectionContext | None = None,
    ) -> PrefetchKey:
        """按 key 去重一次后台 retrieval；不同 key 可同时执行。"""
        query = _bounded_recall_query(query)
        key = self._key(
            session_id,
            query,
            limit=limit,
            intent=intent,
            as_of=as_of,
            known_as_of=known_as_of,
            namespace=namespace,
            token_budget=token_budget,
            projection_version=projection_version,
            injection_context=injection_context,
        )
        now = self._clock()
        future: Future[None] | None = None
        with self._lock:
            self._prune_locked(now)
            entry = self._expire_locked(key, now)
            if entry is not None and entry.status in {"pending", "completed"}:
                return key
            if self._closed:
                closed_error = PrefetchClosedError("prefetch cache is shut down")
                self._record_rejection_locked(
                    key,
                    PrefetchEntry(
                        "expired",
                        None,
                        now,
                        now + self.ttl_seconds,
                        type(closed_error).__name__,
                    ),
                )
                self._closed_rejections += 1
                self._last_error = type(closed_error).__name__
                return key
            if len(self._futures) >= self.max_pending:
                overload_error = PrefetchOverloadedError("prefetch admission queue is at capacity")
                self._record_rejection_locked(
                    key,
                    PrefetchEntry(
                        "expired",
                        None,
                        now,
                        now + self.ttl_seconds,
                        type(overload_error).__name__,
                    ),
                )
                self._overload_rejections += 1
                self._last_error = type(overload_error).__name__
                logger.warning("Hermes memory prefetch rejected because admission capacity is full")
                return key
            self._rejections.pop(key, None)
            if key not in self._values:
                self._make_room_locked()
            pending = PrefetchEntry("pending", None, now, None)
            self._values[key] = pending
            try:
                future = self._executor.submit(
                    self._fetch,
                    key,
                    pending,
                    query,
                )
                self._futures.add(future)
                self._future_keys[future] = key
            except Exception as error:
                if self._values.get(key) is pending:
                    self._values[key] = PrefetchEntry(
                        "expired",
                        None,
                        pending.created_at,
                        now,
                        type(error).__name__,
                    )
                    self._record_failure_locked(error, schema=False)
                logger.warning(
                    "Hermes memory prefetch task failed to queue",
                    exc_info=True,
                )
        if future is not None:
            future.add_done_callback(self._discard_future)
        return key

    def get(
        self,
        session_id: str,
        query: str,
        *,
        limit: int = 10,
        intent: str | None = None,
        as_of: str | None = None,
        known_as_of: str | None = None,
        namespace: str = "default",
        token_budget: int = 2000,
        projection_version: str | None = None,
        injection_context: InjectionContext | None = None,
    ) -> RetrievalBundle | None:
        """读取完整 key 对应的未过期 receipt-free bundle。"""
        query = _bounded_recall_query(query)
        key = self._key(
            session_id,
            query,
            limit=limit,
            intent=intent,
            as_of=as_of,
            known_as_of=known_as_of,
            namespace=namespace,
            token_budget=token_budget,
            projection_version=projection_version,
            injection_context=injection_context,
        )
        with self._lock:
            entry = self._expire_locked(key, self._clock())
            if entry is None or entry.status != "completed":
                return None
            return entry.bundle

    def fetch_now(
        self,
        session_id: str,
        query: str,
        *,
        limit: int = 10,
        intent: str | None = None,
        as_of: str | None = None,
        known_as_of: str | None = None,
        namespace: str = "default",
        token_budget: int = 2000,
        projection_version: str | None = None,
        injection_context: InjectionContext | None = None,
    ) -> RetrievalBundle | None:
        """缓存 miss 时执行一次受熔断器和短超时保护的同步 retrieval。"""
        query = _bounded_recall_query(query)
        key = self._key(
            session_id,
            query,
            limit=limit,
            intent=intent,
            as_of=as_of,
            known_as_of=known_as_of,
            namespace=namespace,
            token_budget=token_budget,
            projection_version=projection_version,
            injection_context=injection_context,
        )
        with self._lock:
            entry = self._expire_locked(key, self._clock())
            if entry is not None and entry.status == "completed":
                return entry.bundle
            closed = self._closed
        if closed:
            error = PrefetchClosedError("prefetch cache is shut down")
            self._handle_retrieval_failure(error)
            return None

        try:
            bundle = self._retrieve(key, query, bounded=True)
        except Exception as error:
            self._handle_retrieval_failure(error)
            return None

        now = self._clock()
        with self._lock:
            self._consecutive_failures = 0
            current = self._expire_locked(key, now)
            if current is not None and current.status == "completed":
                return current.bundle
            if current is None:
                try:
                    self._make_room_locked()
                except RuntimeError:
                    return bundle
            if current is None or current.status != "pending":
                self._values[key] = PrefetchEntry(
                    "completed",
                    bundle,
                    now,
                    now + self.ttl_seconds,
                )
        return bundle

    def inspect(
        self,
        session_id: str,
        query: str,
        *,
        limit: int = 10,
        intent: str | None = None,
        as_of: str | None = None,
        known_as_of: str | None = None,
        namespace: str = "default",
        token_budget: int = 2000,
        projection_version: str | None = None,
        injection_context: InjectionContext | None = None,
    ) -> PrefetchEntry | None:
        """返回 key 的可观测状态快照，读取时同步推进过期状态。"""
        query = _bounded_recall_query(query)
        key = self._key(
            session_id,
            query,
            limit=limit,
            intent=intent,
            as_of=as_of,
            known_as_of=known_as_of,
            namespace=namespace,
            token_budget=token_budget,
            projection_version=projection_version,
            injection_context=injection_context,
        )
        with self._lock:
            now = self._clock()
            self._prune_rejections_locked(now)
            return self._expire_locked(key, now) or self._rejections.get(key)

    def invalidate_session(self, session_id: str) -> None:
        """清理指定会话；仍在运行的旧 fetch 完成后不会重新写回。"""
        with self._lock:
            futures = tuple(future for future, key in self._future_keys.items() if key.session == session_id)
            keys = [key for key in self._values if key.session == session_id]
            for key in keys:
                del self._values[key]
            rejected_keys = [key for key in self._rejections if key.session == session_id]
            for key in rejected_keys:
                del self._rejections[key]
        for future in futures:
            future.cancel()

    def drain(self, timeout: float) -> None:
        """在总 timeout 内等待当前已排队的全部不同 key 任务。"""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            with self._lock:
                futures = tuple(self._futures)
            if not futures:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            done, not_done = wait(futures, timeout=remaining)
            with self._lock:
                self._futures.difference_update(done)
                for future in done:
                    self._future_keys.pop(future, None)
            if not not_done:
                continue
            return

    def shutdown(self, timeout: float) -> None:
        """停止接收新任务、取消尚未运行的任务，并有界等待运行中任务。"""
        with self._lock:
            self._closed = True
            futures = tuple((future, self._future_keys.get(future)) for future in self._futures)
        cancelled: list[tuple[Future[None], PrefetchKey]] = []
        for future, key in futures:
            if key is not None and future.cancel():
                cancelled.append((future, key))
        now = self._clock()
        with self._lock:
            for _, key in cancelled:
                entry = self._values.get(key)
                if entry is not None and entry.status == "pending":
                    self._values[key] = PrefetchEntry(
                        "expired",
                        None,
                        entry.created_at,
                        now,
                        "PrefetchClosedError",
                    )
        self.drain(timeout)
        self._executor.shutdown(wait=False, cancel_futures=True)

    def health(self) -> dict[str, int | str | bool | None]:
        """返回不含 query/session 正文的缓存状态与失败计数。"""
        with self._lock:
            self._prune_locked(self._clock())
            states = {"pending": 0, "completed": 0, "expired": 0}
            for entry in self._values.values():
                states[entry.status] += 1
            states["expired"] += len(self._rejections)
            return {
                **states,
                "cached_entries": len(self._values),
                "rejection_entries": len(self._rejections),
                "queued_tasks": len(self._futures),
                "max_pending": self.max_pending,
                "retrieval_failures": self._retrieval_failures,
                "schema_failures": self._schema_failures,
                "overload_rejections": self._overload_rejections,
                "closed_rejections": self._closed_rejections,
                "consecutive_failures": self._consecutive_failures,
                "closed": self._closed,
                "last_error": self._last_error,
            }

    def _fetch(
        self,
        key: PrefetchKey,
        pending: PrefetchEntry,
        query: str,
    ) -> None:
        try:
            bundle = self._retrieve(key, query, bounded=False)
        except Exception as error:
            with self._lock:
                if self._values.get(key) is pending:
                    self._values[key] = PrefetchEntry(
                        "expired",
                        None,
                        pending.created_at,
                        self._clock(),
                        type(error).__name__,
                    )
            self._handle_retrieval_failure(error)
            return

        with self._lock:
            self._consecutive_failures = 0
            if self._values.get(key) is pending:
                self._values[key] = PrefetchEntry(
                    "completed",
                    bundle,
                    pending.created_at,
                    self._clock() + self.ttl_seconds,
                )

    def _retrieve(
        self,
        key: PrefetchKey,
        query: str,
        *,
        bounded: bool,
    ) -> RetrievalBundle:
        if not self.client.can_call():
            raise RuntimeError("memory daemon circuit is open")
        payload = {
            "query": query,
            "session_id": key.session or None,
            "limit": key.limit,
            "intent": key.intent,
            "as_of": key.as_of,
            "known_as_of": key.known_as_of,
            "namespace": key.namespace,
            "token_budget": key.token_budget,
            "context_mode": "packed",
            "delivery_purpose": key.delivery_purpose,
            "experiment_variant": key.experiment_variant,
            "echo_variant": key.echo_variant,
            "freshness_variant": key.freshness_variant,
            "policy_versions": dict(key.policy_versions),
            "rendering_now": key.freshness_time_bucket,
            "freshness_time_bucket": key.freshness_time_bucket,
        }
        if bounded:
            response = self.client.recall_bundle(
                payload,
                timeout=min(self.client.timeout, self.on_demand_timeout_seconds),
                retry=False,
            )
        else:
            response = self.client.recall_bundle(payload)
        response_payload = response.json()
        if not isinstance(response_payload, Mapping):
            raise TypeError("retrieval bundle response must be an object")
        raw_bundle = response_payload.get("retrieval_bundle")
        if not isinstance(raw_bundle, Mapping):
            raise TypeError("retrieval bundle response is missing retrieval_bundle")
        bundle = retrieval_bundle_from_dict(raw_bundle)
        self.client.on_success()
        return bundle

    def _handle_retrieval_failure(self, error: Exception) -> None:
        self.client.on_failure()
        schema_failure = isinstance(error, UnknownSchemaMajorError)
        with self._lock:
            consecutive_failures = self._record_failure_locked(error, schema=schema_failure)
        log = logger.error if consecutive_failures >= PREFETCH_ERROR_THRESHOLD else logger.warning
        status_error = find_http_status_error(error)
        if status_error is not None and status_error.response.status_code == 422:
            server_version = sanitize_http_response_body(
                status_error.response.headers.get(HL_MEM_VERSION_HEADER, "unknown"),
                limit=64,
            )
            log(
                "Hermes memory prefetch rejected status=422 client_version=%s server_version=%s "
                "consecutive_failures=%d validation_detail=%s",
                __version__,
                server_version,
                consecutive_failures,
                validation_response_body(status_error.response),
                exc_info=True,
            )
            return
        log(
            "Hermes memory prefetch failed; bundle unavailable consecutive_failures=%d",
            consecutive_failures,
            exc_info=True,
        )

    def _discard_future(self, future: Future[None]) -> None:
        with self._lock:
            self._futures.discard(future)
            self._future_keys.pop(future, None)
            self._prune_locked(self._clock())

    def _prune_locked(self, now: float) -> None:
        self._prune_rejections_locked(now)
        for key in tuple(self._values):
            self._expire_locked(key, now)
        while len(self._values) > self.max_entries:
            if not self._evict_oldest_non_pending_locked():
                return

    def _make_room_locked(self) -> None:
        while len(self._values) >= self.max_entries:
            if not self._evict_oldest_non_pending_locked():
                raise RuntimeError("prefetch cache capacity invariant violated")

    def _evict_oldest_non_pending_locked(self) -> bool:
        removable = [
            (
                0 if entry.status == "expired" else 1,
                entry.created_at,
                key,
            )
            for key, entry in self._values.items()
            if entry.status != "pending"
        ]
        if not removable:
            return False
        _, _, oldest_key = min(
            removable,
            key=lambda candidate: (candidate[0], candidate[1]),
        )
        del self._values[oldest_key]
        return True

    def _record_rejection_locked(
        self,
        key: PrefetchKey,
        entry: PrefetchEntry,
    ) -> None:
        cached = self._values.get(key)
        if cached is not None and cached.status == "expired":
            del self._values[key]
        self._rejections.pop(key, None)
        self._rejections[key] = entry
        while len(self._rejections) > self._max_rejections:
            self._rejections.popitem(last=False)

    def _prune_rejections_locked(self, now: float) -> None:
        expired_keys = [
            key for key, entry in self._rejections.items() if entry.expires_at is not None and entry.expires_at <= now
        ]
        for key in expired_keys:
            del self._rejections[key]

    def _expire_locked(
        self,
        key: PrefetchKey,
        now: float,
    ) -> PrefetchEntry | None:
        entry = self._values.get(key)
        if (
            entry is not None
            and entry.status == "completed"
            and entry.expires_at is not None
            and entry.expires_at <= now
        ):
            entry = replace(
                entry,
                status="expired",
                bundle=None,
            )
            self._values[key] = entry
        return entry

    def _record_failure_locked(
        self,
        error: Exception,
        *,
        schema: bool,
    ) -> int:
        self._retrieval_failures += 1
        self._consecutive_failures += 1
        if schema:
            self._schema_failures += 1
        self._last_error = type(error).__name__
        return self._consecutive_failures

    def _key(
        self,
        session_id: str,
        query: str,
        *,
        limit: int,
        intent: str | None,
        as_of: str | None,
        known_as_of: str | None,
        namespace: str,
        token_budget: int,
        projection_version: str | None,
        injection_context: InjectionContext | None,
    ) -> PrefetchKey:
        if limit < 1:
            raise ValueError("limit must be positive")
        if token_budget < 1:
            raise ValueError("token_budget must be positive")
        if not namespace:
            raise ValueError("namespace must be non-empty")
        version = projection_version or self.projection_version
        if not version:
            raise ValueError("projection_version must be non-empty")
        context = injection_context or InjectionContext.create(
            delivery_purpose="passive_injection",
            rendering_now=datetime.now(timezone.utc).isoformat(),
        )
        return PrefetchKey(
            session=session_id,
            query_hash=hashlib.sha256(query.encode("utf-8")).hexdigest(),
            limit=limit,
            intent=intent,
            as_of=as_of,
            known_as_of=known_as_of,
            namespace=namespace,
            token_budget=token_budget,
            projection_version=version,
            delivery_purpose=context.delivery_purpose,
            experiment_variant=context.experiment_variant,
            echo_variant=context.echo_variant,
            freshness_variant=context.freshness_variant,
            policy_versions=context.policy_versions,
            freshness_time_bucket=context.freshness_time_bucket,
        )


def _bounded_recall_query(query: str) -> str:
    """对齐服务端 RecallInput.query 的 2000 字符上限。"""
    return query[:MAX_RECALL_QUERY_LENGTH]
