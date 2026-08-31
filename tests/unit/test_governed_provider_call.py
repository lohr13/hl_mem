from __future__ import annotations

import json

import httpx
import pytest

from hl_mem.errors import UsageLimitExceededError
from hl_mem.monitoring.metrics import ProviderMetrics
from hl_mem.observability.usage import UsageAmount, UsageGovernor, UsageIdentity, UsageLimits
from hl_mem.plugins.contracts import ProviderCapability, ProviderRequest, ProviderResponse
from hl_mem.plugins.proxies import GovernedProviderCall
from hl_mem.plugins.transport import ProviderTransport


class RecordingAudit:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def emit(self, phase: str, action: str, outcome: str, **values: object) -> bool:
        self.events.append({"phase": phase, "action": action, "outcome": outcome, **values})
        return True


def _request() -> ProviderRequest:
    return ProviderRequest(
        "POST",
        "https://provider.example.test/run",
        {"Authorization": "Bearer top-secret"},
        {"prompt": "private memory"},
        5.0,
    )


def _identity() -> UsageIdentity:
    return UsageIdentity(ProviderCapability.LLM, "extract", "hl-mem.builtin", "dashscope", "qwen")


def _governed(tmp_path, handler, *, estimator=None, cost_limit: int = 0):  # type: ignore[no-untyped-def]
    governor = UsageGovernor(tmp_path / "usage.db", UsageLimits(0, 0, cost_limit))
    metrics = ProviderMetrics()
    audit = RecordingAudit()
    transport = ProviderTransport(
        httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _delay: None,
    )
    return (
        GovernedProviderCall(_identity(), governor, transport, metrics, audit, estimator=estimator),
        governor,
        metrics,
        audit,
    )


def _price_book(tmp_path, *, model: str = "qwen", source_url: str = "https://pricing.example.test"):
    from hl_mem.observability.pricing import UsagePriceBook

    path = tmp_path / f"{model}.pricing.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "currency": "CNY",
                "effective_date": "2026-09-01",
                "source_urls": [source_url],
                "rules": [
                    {
                        "capability": "llm",
                        "provider": "dashscope",
                        "model": model,
                        "rates_microunits": {
                            "request": 10,
                            "million_input_tokens": 1,
                            "million_output_tokens": 0,
                            "embedding_item": 0,
                            "rerank_document": 0,
                            "image": 0,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return UsagePriceBook.load(path)


def _parse(response: ProviderResponse) -> tuple[str, UsageAmount]:
    assert response.json_body["ok"] is True
    return "ok", UsageAmount(requests=1, input_tokens=3, cost_microunits=2)


def test_retry_marks_and_accounts_for_each_actual_attempt(tmp_path) -> None:
    sends = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sends
        sends += 1
        if sends == 1:
            raise httpx.ConnectTimeout("slow", request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    governed, governor, metrics, audit = _governed(tmp_path, handler)
    result = governed.execute(
        _request(),
        UsageAmount(requests=1, input_tokens=3, cost_microunits=2),
        _parse,
        max_attempts=2,
    )

    assert result == "ok"
    assert governor.snapshot()["settled"]["requests"] == 2
    assert governor.snapshot()["settled"]["input_tokens"] == 6
    metric_snapshot = metrics.snapshot()
    assert metric_snapshot["calls"] == 1
    assert metric_snapshot["attempts"] == 2
    assert metric_snapshot["usage"]["requests"] == 2
    assert metric_snapshot["providers"] == [
        {
            "plugin_id": "hl-mem.builtin",
            "provider": "dashscope",
            "model": "qwen",
            "calls": 1,
        }
    ]
    assert len(audit.events) == 1


def test_pre_send_adapter_failure_releases_reservation(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not run")

    governed, governor, metrics, audit = _governed(tmp_path, handler)

    def invalid_request() -> ProviderRequest:
        raise ValueError("invalid request")

    with pytest.raises(ValueError, match="invalid request"):
        governed.execute_factory(
            invalid_request,
            UsageAmount(requests=1, input_tokens=3, cost_microunits=2),
            _parse,
            max_attempts=2,
        )

    assert governor.snapshot()["reserved"]["requests"] == 0
    assert governor.snapshot()["settled"]["requests"] == 0
    assert metrics.snapshot()["calls"] == 1
    assert len(audit.events) == 1


@pytest.mark.parametrize("failure", ("http_400", "timeout", "parser"))
def test_post_send_failures_settle_only_actual_attempt_estimates(tmp_path, failure: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "http_400":
            return httpx.Response(400, json={"error": "bad"}, request=request)
        if failure == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    governed, governor, _metrics, _audit = _governed(tmp_path, handler)

    def parser(response: ProviderResponse) -> tuple[str, UsageAmount]:
        if failure == "parser":
            raise ValueError("bad response")
        return _parse(response)

    max_attempts = 2
    with pytest.raises(Exception):
        governed.execute(
            _request(),
            UsageAmount(requests=1, input_tokens=3, cost_microunits=2),
            parser,
            max_attempts=max_attempts,
        )

    expected_attempts = 2 if failure == "timeout" else 1
    assert governor.snapshot()["settled"]["requests"] == expected_attempts
    assert governor.snapshot()["settled"]["input_tokens"] == expected_attempts * 3
    assert governor.snapshot()["reserved"]["requests"] == 0


def test_actual_usage_over_reservation_is_recorded(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True}, request=request)

    governed, governor, _metrics, _audit = _governed(tmp_path, handler)

    def parser(_response: ProviderResponse) -> tuple[str, UsageAmount]:
        return "ok", UsageAmount(requests=1, input_tokens=20, cost_microunits=10)

    governed.execute(
        _request(),
        UsageAmount(requests=1, input_tokens=3, cost_microunits=2),
        parser,
        max_attempts=1,
    )

    assert governor.snapshot()["settled"]["input_tokens"] == 20


def test_metric_and_audit_are_single_safe_final_events(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True}, request=request)

    governed, _governor, metrics, audit = _governed(tmp_path, handler)
    governed.execute(
        _request(),
        UsageAmount(requests=1, input_tokens=3, cost_microunits=2),
        _parse,
        max_attempts=1,
    )

    calls = metrics.snapshot()
    assert calls["calls"] == 1
    assert calls["failures"] == 0
    assert len(audit.events) == 1
    rendered = repr(audit.events)
    assert "top-secret" not in rendered
    assert "private memory" not in rendered


def test_active_money_limit_rejects_an_unpriced_model_before_request_creation(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not run")

    governed, _governor, _metrics, _audit = _governed(
        tmp_path,
        handler,
        estimator=_price_book(tmp_path, model="other"),
        cost_limit=1_000_000,
    )

    with pytest.raises(UsageLimitExceededError, match="cost"):
        governed.execute_factory(
            lambda: (_ for _ in ()).throw(AssertionError("request factory must not run")),
            UsageAmount(requests=1, input_tokens=1),
            _parse,
            max_attempts=1,
        )


def test_estimator_prices_reservation_and_settles_actual_usage(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True}, request=request)

    governed, governor, metrics, audit = _governed(tmp_path, handler, estimator=_price_book(tmp_path))

    def parser(_response: ProviderResponse) -> tuple[str, UsageAmount]:
        return "ok", UsageAmount(requests=1, input_tokens=2)

    governed.execute(_request(), UsageAmount(requests=1, input_tokens=1), parser, max_attempts=1)

    assert governor.snapshot()["settled"]["cost_microunits"] == 11
    assert metrics.snapshot()["usage"]["cost_microunits"] == 11
    assert metrics.snapshot()["price_book_fingerprint"] == governed.estimator.fingerprint
    detail = audit.events[0]["detail"]
    assert detail["cost_microunits"] == 11
    assert detail["price_book_fingerprint"] == governed.estimator.fingerprint


def test_retry_pricing_ceilings_are_applied_per_attempt(tmp_path) -> None:
    sends = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sends
        sends += 1
        if sends == 1:
            raise httpx.ConnectTimeout("slow", request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    governed, governor, _metrics, _audit = _governed(tmp_path, handler, estimator=_price_book(tmp_path))

    def parser(_response: ProviderResponse) -> tuple[str, UsageAmount]:
        return "ok", UsageAmount(requests=1, input_tokens=1)

    governed.execute(_request(), UsageAmount(requests=1, input_tokens=1), parser, max_attempts=2)

    assert governor.snapshot()["settled"]["cost_microunits"] == 22


def test_no_estimator_preserves_existing_request_token_and_cost_behavior(tmp_path) -> None:
    sends = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sends
        sends += 1
        if sends == 1:
            raise httpx.ConnectTimeout("slow", request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    governed, governor, _metrics, audit = _governed(tmp_path, handler)

    def parser(_response: ProviderResponse) -> tuple[str, UsageAmount]:
        return "ok", UsageAmount(requests=1, input_tokens=5, cost_microunits=10)

    governed.execute(
        _request(),
        UsageAmount(requests=1, input_tokens=3, cost_microunits=2),
        parser,
        max_attempts=2,
    )

    settled = governor.snapshot()["settled"]
    assert settled["requests"] == 2
    assert settled["input_tokens"] == 8
    assert settled["cost_microunits"] == 12
    assert "price_book_fingerprint" not in audit.events[0]["detail"]


def test_price_book_source_and_path_never_enter_audit(tmp_path) -> None:
    source_url = "https://pricing.example.test/private-source"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True}, request=request)

    governed, _governor, _metrics, audit = _governed(
        tmp_path,
        handler,
        estimator=_price_book(tmp_path, source_url=source_url),
    )
    governed.execute(_request(), UsageAmount(requests=1), _parse, max_attempts=1)

    rendered = repr(audit.events)
    assert source_url not in rendered
    assert str(tmp_path) not in rendered
