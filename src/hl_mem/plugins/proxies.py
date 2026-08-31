"""Provider 调用的预留、执行、结算与安全观测公共算法。"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Generic, TypeVar

from hl_mem.errors import ProviderCallError
from hl_mem.monitoring.metrics import DEFAULT_PROVIDER_METRICS, ProviderCall, ProviderMetrics
from hl_mem.observability.audit import current_audit
from hl_mem.observability.usage import UsageAmount, UsageGovernor, UsageIdentity
from hl_mem.plugins.contracts import ProviderRequest, ProviderResponse
from hl_mem.plugins.transport import ProviderTransport

T = TypeVar("T")


class GovernedProviderCall(Generic[T]):
    """对一次逻辑 Provider 调用执行唯一的治理终结流程。"""

    def __init__(
        self,
        identity: UsageIdentity,
        governor: UsageGovernor,
        transport: ProviderTransport,
        metrics: ProviderMetrics | None = None,
        audit: Any = None,
    ) -> None:
        self.identity = identity
        self.governor = governor
        self.transport = transport
        self.metrics = metrics or DEFAULT_PROVIDER_METRICS
        self.audit = audit if audit is not None else current_audit()

    def execute(
        self,
        request: ProviderRequest,
        estimate: UsageAmount,
        parser: Callable[[ProviderResponse], tuple[T, UsageAmount]],
        *,
        max_attempts: int,
        settlement_status: Callable[[T], str] | None = None,
    ) -> T:
        return self.execute_factory(
            lambda: request,
            estimate,
            parser,
            max_attempts=max_attempts,
            settlement_status=settlement_status,
        )

    def execute_factory(
        self,
        request_factory: Callable[[], ProviderRequest],
        estimate: UsageAmount,
        parser: Callable[[ProviderResponse], tuple[T, UsageAmount]],
        *,
        max_attempts: int,
        settlement_status: Callable[[T], str] | None = None,
    ) -> T:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        reservation = self.governor.reserve(self.identity, estimate.scale(max_attempts))
        started = time.perf_counter()
        marked_attempts = 0

        def mark_attempt(expected_attempt: int) -> None:
            nonlocal marked_attempts
            marked_attempts = self.governor.mark_attempt(reservation.id)
            if marked_attempts != expected_attempt:
                raise RuntimeError("usage attempt sequence diverged from transport attempt sequence")

        try:
            request = request_factory()
        except Exception as error:
            self.governor.release(reservation.id, reason="request_validation_failed")
            self._record(
                "error",
                (time.perf_counter() - started) * 1000,
                reservation.id,
                UsageAmount(cost_microunits=0),
                0,
                error,
            )
            raise

        try:
            response = self.transport.execute(request, max_attempts=max_attempts, on_attempt=mark_attempt)
            value, actual = parser(response)
            if not isinstance(actual, UsageAmount):
                raise TypeError("Provider parser must return UsageAmount as its second value")
        except Exception as error:
            conservative = estimate.scale(marked_attempts)
            if marked_attempts > 0:
                self.governor.settle_unknown(
                    reservation.id,
                    conservative,
                    status="error",
                    latency_ms=(time.perf_counter() - started) * 1000,
                    error_class=self._error_class(error),
                )
            else:
                self.governor.release(reservation.id, reason="pre_send_failure")
            self._record(
                "error",
                (time.perf_counter() - started) * 1000,
                reservation.id,
                conservative,
                marked_attempts,
                error,
            )
            raise

        total_actual = estimate.scale(max(0, response.attempts - 1)) + actual
        latency_ms = (time.perf_counter() - started) * 1000
        usage_status = settlement_status(value) if settlement_status is not None else "success"
        try:
            self.governor.settle(
                reservation.id,
                total_actual,
                status=usage_status,
                latency_ms=latency_ms,
            )
        except Exception as error:
            self._record("error", latency_ms, reservation.id, total_actual, response.attempts, error)
            raise
        self._record("success", latency_ms, reservation.id, total_actual, response.attempts, None)
        return value

    @staticmethod
    def _error_class(error: Exception) -> str:
        return error.category if isinstance(error, ProviderCallError) else type(error).__name__

    def _record(
        self,
        status: str,
        latency_ms: float,
        reservation_id: str,
        usage: UsageAmount,
        attempts: int,
        error: Exception | None,
    ) -> None:
        error_class = self._error_class(error) if error is not None else None
        http_status = error.http_status if isinstance(error, ProviderCallError) else None
        provider_code = error.provider_code if isinstance(error, ProviderCallError) else None
        call = ProviderCall(
            self.identity.capability.value,
            self.identity.operation,
            status,
            latency_ms,
            error_class=error_class,
            http_status=http_status,
            provider_code=provider_code,
            plugin_id=self.identity.plugin_id,
            provider=self.identity.provider,
            model=self.identity.model,
            attempts=attempts,
            requests=usage.requests,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            embedding_items=usage.embedding_items,
            rerank_documents=usage.rerank_documents,
            images=usage.images,
            cost_microunits=usage.cost_microunits,
        )
        try:
            self.metrics.record(call)
        except Exception:
            pass
        detail: dict[str, object] = {
            "reservation_id": reservation_id,
            "capability": self.identity.capability.value,
            "operation": self.identity.operation,
            "plugin_id": self.identity.plugin_id,
            "provider": self.identity.provider,
            "model": self.identity.model,
            "attempts": attempts,
            "requests": usage.requests,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "embedding_items": usage.embedding_items,
            "rerank_documents": usage.rerank_documents,
            "images": usage.images,
            "cost_microunits": usage.cost_microunits,
        }
        if error_class is not None:
            detail["error_class"] = error_class
        if http_status is not None:
            detail["http_status"] = http_status
        if provider_code is not None:
            detail["provider_code"] = provider_code
        try:
            self.audit.emit(
                "provider",
                "call",
                status,
                duration_us=int(latency_ms * 1000),
                detail=detail,
            )
        except Exception:
            pass


__all__ = ["GovernedProviderCall"]
