"""Read-only diagnostics for Provider discovery, governance, and model paths."""

from __future__ import annotations

import sqlite3
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from hl_mem import components
from hl_mem.diagnostics import CheckResult, CheckStatus
from hl_mem.errors import ConfigurationError
from hl_mem.llm.types import LLMMessage, LLMRequest
from hl_mem.observability.pricing import UsageCostEstimator, UsagePriceBook
from hl_mem.observability.usage import USAGE_LEDGER_SCHEMA_VERSION, default_usage_ledger_path
from hl_mem.plugins.runtime import ProviderRuntime
from hl_mem.settings import Settings, is_placeholder_secret

_ESTIMATOR_UNSET = object()


def check_provider_plugins(settings: Settings) -> list[CheckResult]:
    try:
        registry = components.make_provider_registry(settings)
        capabilities = ", ".join(
            f"{item['capability']}:{item['name']}@{item['plugin_id']}" for item in registry.health_snapshot()
        )
    except Exception:
        resolution = CheckResult(CheckStatus.FAIL, "Provider 插件", "Provider registry check failed")
    else:
        resolution = CheckResult(CheckStatus.OK, "Provider 插件", capabilities or "未注册 Provider")
    if settings.plugins_enabled:
        trust = CheckResult(
            CheckStatus.WARN,
            "Provider 信任",
            "启用的第三方 Provider 在宿主进程内运行，必须仅安装可信发行包：" + ", ".join(settings.plugins_enabled),
        )
    else:
        trust = CheckResult(CheckStatus.OK, "Provider 信任", "仅使用内置 Provider；未加载第三方代码")
    return [resolution, trust]


def check_usage_ledger(settings: Settings) -> CheckResult:
    path = default_usage_ledger_path(settings.database_path)
    if not path.is_file():
        return CheckResult(CheckStatus.WARN, "Provider 用量账本", "尚未创建；首次真实 Provider 调用时初始化")
    try:
        with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version != USAGE_LEDGER_SCHEMA_VERSION:
                return CheckResult(
                    CheckStatus.FAIL,
                    "Provider 用量账本",
                    f"schema={version}，要求 {USAGE_LEDGER_SCHEMA_VERSION}",
                )
            now = datetime.now(timezone.utc).isoformat()
            row = connection.execute(
                "SELECT COALESCE(SUM(CASE WHEN attempts=0 THEN 1 ELSE 0 END),0),"
                "COALESCE(SUM(CASE WHEN attempts>0 THEN 1 ELSE 0 END),0) "
                "FROM usage_reservations WHERE state='active' AND lease_expires_at<?",
                (now,),
            ).fetchone()
            expired_unsent, expired_ambiguous = int(row[0]), int(row[1])
    except (OSError, sqlite3.Error, TypeError, ValueError) as error:
        return CheckResult(CheckStatus.FAIL, "Provider 用量账本", f"无法只读检查：{error}")
    status = CheckStatus.WARN if expired_unsent or expired_ambiguous else CheckStatus.OK
    return CheckResult(
        status,
        "Provider 用量账本",
        f"schema={version}；expired_unsent={expired_unsent}；expired_ambiguous={expired_ambiguous}；"
        "doctor 未执行恢复",
    )


def check_usage_price_book(settings: Settings) -> CheckResult | None:
    return validated_usage_price_book(settings)[0]


def validated_usage_price_book(
    settings: Settings,
) -> tuple[CheckResult | None, UsagePriceBook | None]:
    if settings.usage_price_book_path is None:
        return None, None
    try:
        price_book = UsagePriceBook.load(Path(settings.usage_price_book_path))
    except ConfigurationError:
        return (
            CheckResult(
                CheckStatus.FAIL,
                "Provider 价格表",
                "configured=true；validation failed",
                code="usage_price_book",
            ),
            None,
        )
    return (
        CheckResult(
            CheckStatus.OK,
            "Provider 价格表",
            f"configured=true；fingerprint={price_book.fingerprint}",
            code="usage_price_book",
        ),
        price_book,
    )


def check_embedding(settings: Settings, runtime: ProviderRuntime | None = None) -> CheckResult:
    if settings.embedder_mode == "fake":
        return CheckResult(CheckStatus.WARN, "Embedding API", "embedder=fake，跳过")
    embedder = None
    try:
        embedder = components.make_embedder(settings, runtime=runtime)
        embedder.embed_one("ping")
        return CheckResult(CheckStatus.OK, "Embedding API", "请求成功")
    except Exception:
        return CheckResult(CheckStatus.FAIL, "Embedding API", "minimal request failed")
    finally:
        close = getattr(embedder, "close", None)
        if callable(close):
            close()


def check_llm(settings: Settings, runtime: ProviderRuntime | None = None) -> CheckResult:
    if settings.extractor_mode == "fake":
        return CheckResult(CheckStatus.WARN, "LLM API", "extractor=fake，跳过")
    client = None
    try:
        client = components.make_llm_client(settings, operation="doctor", runtime=runtime)
        client.complete(
            LLMRequest([LLMMessage("user", "Return only the smallest valid JSON object: {}")]),
            timeout_seconds=settings.llm_timeout,
        )
        return CheckResult(CheckStatus.OK, "LLM API", "请求成功")
    except Exception:
        return CheckResult(CheckStatus.FAIL, "LLM API", "minimal request failed")
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


def check_reranker(settings: Settings, runtime: ProviderRuntime | None = None) -> CheckResult:
    if settings.reranker_mode == "off":
        return CheckResult(CheckStatus.WARN, "Reranker API", "reranker=off，跳过")
    if is_placeholder_secret(settings.reranker_api_key):
        return CheckResult(CheckStatus.FAIL, "Reranker API", "缺少有效 API key")
    reranker = None
    try:
        reranker = components.make_reranker(settings, runtime=runtime)
        if reranker is None:
            return CheckResult(CheckStatus.FAIL, "Reranker API", "reranker 未启用")
        results = reranker.rerank("ping", ["ping"], top_n=1)
    except Exception:
        return CheckResult(CheckStatus.FAIL, "Reranker API", "minimal request failed")
    finally:
        close = getattr(reranker, "close", None)
        if callable(close):
            close()
    if not results:
        return CheckResult(CheckStatus.FAIL, "Reranker API", "最小请求未返回结果")
    return CheckResult(CheckStatus.OK, "Reranker API", "请求成功")


def probe_model_components(
    settings: Settings,
    *,
    estimator: UsageCostEstimator | None | object = _ESTIMATOR_UNSET,
) -> list[CheckResult]:
    """Probe every model path enabled by a prospective production configuration."""
    with tempfile.TemporaryDirectory(prefix="hl-mem-provider-probe-", ignore_cleanup_errors=True) as temporary:
        probe_settings = replace(
            settings,
            database_path=str(Path(temporary) / "probe.db"),
            llm_max_tokens=64,
        )
        try:
            if estimator is _ESTIMATOR_UNSET:
                runtime = components.create_provider_runtime(probe_settings)
            else:
                runtime = components.create_provider_runtime(
                    probe_settings,
                    _validated_estimator=cast(UsageCostEstimator | None, estimator),
                )
        except Exception:
            return [
                CheckResult(
                    CheckStatus.FAIL,
                    "Provider model runtime",
                    "initialization failed",
                    code="model_runtime",
                )
            ]
        try:
            results = [check_llm(probe_settings, runtime), check_embedding(probe_settings, runtime)]
            if probe_settings.reranker_mode in {"on", "real"}:
                results.append(check_reranker(probe_settings, runtime))
            return results
        finally:
            runtime.close()
