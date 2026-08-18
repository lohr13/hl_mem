"""Shared, side-effect-free injection-governance request context."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

DeliveryPurpose = Literal["passive_injection", "active_recall", "api"]
INJECTION_CONTEXT_VERSION = "injection-v1"
ECHO_POLICY_VERSION = "same-session-v1"
FRESHNESS_POLICY_VERSION = "risk-age-v1"
DEFAULT_POLICY_VERSIONS = {
    "echo": ECHO_POLICY_VERSION,
    "freshness": FRESHNESS_POLICY_VERSION,
}
DELIVERY_PURPOSES: tuple[DeliveryPurpose, ...] = ("passive_injection", "active_recall", "api")


def _utc_rendering_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("rendering_now must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("rendering_now must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _non_empty(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must be non-empty")
    if len(normalized) > 100:
        raise ValueError(f"{field_name} must be at most 100 characters")
    return normalized


@dataclass(frozen=True, slots=True)
class InjectionContext:
    """All request-scoped values that may affect passive memory injection."""

    delivery_purpose: DeliveryPurpose
    experiment_variant: str
    echo_variant: str
    freshness_variant: str
    policy_versions: tuple[tuple[str, str], ...]
    rendering_now: str
    freshness_time_bucket: str

    @classmethod
    def create(
        cls,
        *,
        delivery_purpose: DeliveryPurpose = "api",
        experiment_variant: str = "control",
        echo_variant: str = "off",
        freshness_variant: str = "off",
        policy_versions: Mapping[str, str] | None = None,
        rendering_now: str,
    ) -> "InjectionContext":
        if delivery_purpose not in DELIVERY_PURPOSES:
            raise ValueError(f"unsupported delivery purpose: {delivery_purpose}")
        normalized_versions = tuple(
            sorted(
                (
                    _non_empty(str(name), "policy_versions key"),
                    _non_empty(str(version), "policy_versions value"),
                )
                for name, version in (policy_versions or DEFAULT_POLICY_VERSIONS).items()
            )
        )
        utc_now = _utc_rendering_time(rendering_now)
        bucket = utc_now.replace(minute=0, second=0, microsecond=0)
        return cls(
            delivery_purpose=delivery_purpose,
            experiment_variant=_non_empty(experiment_variant, "experiment_variant"),
            echo_variant=_non_empty(echo_variant, "echo_variant"),
            freshness_variant=_non_empty(freshness_variant, "freshness_variant"),
            policy_versions=normalized_versions,
            rendering_now=utc_now.isoformat(timespec="seconds"),
            freshness_time_bucket=bucket.isoformat(timespec="seconds"),
        )

    def envelope(self) -> dict[str, object]:
        """Return a safe trace/replay envelope without query, content, or session identifiers."""
        return {
            "schema_version": INJECTION_CONTEXT_VERSION,
            "delivery_purpose": self.delivery_purpose,
            "experiment_variant": self.experiment_variant,
            "policy_versions": dict(self.policy_versions),
            "variants": {"echo": self.echo_variant, "freshness": self.freshness_variant},
            "rendering_now": self.rendering_now,
            "freshness_time_bucket": self.freshness_time_bucket,
        }


def injection_governance_snapshot() -> dict[str, object]:
    """Return the stable public health envelope before policy-specific metrics are added."""
    return {
        "schema_version": INJECTION_CONTEXT_VERSION,
        "delivery_purposes": list(DELIVERY_PURPOSES),
        "policy_versions": dict(DEFAULT_POLICY_VERSIONS),
        "echo_suppression": {"mode": "off"},
        "freshness_annotation": {"mode": "off"},
    }
