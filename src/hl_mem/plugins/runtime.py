"""Process-scoped Provider registry, transport, and usage-governance ownership."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from hl_mem.config.models import Settings
from hl_mem.errors import ConfigurationError
from hl_mem.observability.usage import (
    UsageGovernor,
    UsageIdentity,
    UsageLimits,
    default_usage_ledger_path,
)
from hl_mem.plugins.contracts import ProviderCapability, ProviderKey
from hl_mem.plugins.proxies import GovernedProviderCall
from hl_mem.plugins.registry import ProviderRegistry, build_provider_registry
from hl_mem.plugins.transport import ProviderTransport


@dataclass
class ProviderRuntime:
    """Own one immutable registry and one governed transport per process boundary."""

    settings: Settings
    registry: ProviderRegistry
    _governor: UsageGovernor | None
    transport: ProviderTransport
    _client: httpx.Client | None = None
    _owns_client: bool = False

    @property
    def governor(self) -> UsageGovernor:
        if self._governor is None:
            raise ConfigurationError("Provider usage governance is disabled for this runtime")
        return self._governor

    def governed_call(
        self,
        capability: ProviderCapability,
        provider: str,
        operation: str,
        model: str,
    ) -> GovernedProviderCall[Any]:
        key = ProviderKey(capability, provider)
        return GovernedProviderCall(
            UsageIdentity(
                capability=capability,
                operation=operation,
                plugin_id=self.registry.plugin_id_for(key),
                provider=provider,
                model=model,
            ),
            self.governor,
            self.transport,
        )

    def usage_snapshot(self) -> dict[str, object] | None:
        return None if self._governor is None else self._governor.snapshot()

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None


def create_provider_runtime(
    settings: Settings,
    *,
    entry_points: Any = None,
    client: httpx.Client | None = None,
    create_usage: bool = True,
) -> ProviderRuntime:
    """Validate Providers, recover reservations, and create process-owned state."""

    registry = build_provider_registry(settings, entry_points=entry_points)
    governor = None
    if create_usage:
        governor = UsageGovernor(
            default_usage_ledger_path(settings.database_path),
            UsageLimits(
                daily_requests=settings.usage_daily_request_limit,
                daily_tokens=settings.daily_token_limit,
                daily_cost_microunits=settings.usage_daily_cost_limit_microunits,
            ),
            lease_seconds=settings.usage_reservation_lease_seconds,
        )
        governor.recover_expired()
    resolved_client = client or httpx.Client()
    return ProviderRuntime(
        settings=settings,
        registry=registry,
        _governor=governor,
        transport=ProviderTransport(resolved_client),
        _client=resolved_client,
        _owns_client=client is None,
    )


__all__ = ["ProviderRuntime", "create_provider_runtime"]
