"""Host-owned, exact-match Provider usage pricing."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker

from hl_mem.errors import ConfigurationError
from hl_mem.observability.usage_types import UsageAmount, UsageIdentity
from hl_mem.plugins.contracts import ProviderCapability

_V1_CAPABILITIES = ("llm", "embedding", "reranker", "image_describer")
_V1_LABEL_PATTERN = r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$"
_V1_MODEL_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9._:/-]{0,198}[A-Za-z0-9])?$"
_V1_HTTPS_URL_PATTERN = (
    r"^https://(?:[^/?#@\s]+@)?(?:\[[0-9A-Fa-f:.]+\]|[^/?#:@\s]+)(?::[0-9]+)?(?:[/?#].*)?$"
)
_RATE_KEYS = (
    "request",
    "million_input_tokens",
    "million_output_tokens",
    "embedding_item",
    "rerank_document",
    "image",
)
_RATE_TO_UNIT = {
    "request": "requests",
    "million_input_tokens": "input_tokens",
    "million_output_tokens": "output_tokens",
    "embedding_item": "embedding_items",
    "rerank_document": "rerank_documents",
    "image": "images",
}
_PRICE_BOOK_SCHEMA_V1: dict[str, object] = {
    "$id": "https://hl-mem.local/schemas/usage-pricing-v1.json",
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "HL-Mem Provider Usage Price Book",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "currency", "effective_date", "rules"],
    "properties": {
        "schema_version": {"const": 1},
        "currency": {"const": "CNY"},
        "effective_date": {"type": "string", "format": "date"},
        "source_urls": {
            "type": "array",
            "maxItems": 16,
            "items": {
                "type": "string",
                "format": "uri",
                "maxLength": 2048,
                "pattern": _V1_HTTPS_URL_PATTERN,
            },
        },
        "rules": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["capability", "model", "rates_microunits"],
                "properties": {
                    "capability": {"enum": list(_V1_CAPABILITIES)},
                    "provider": {"type": "string", "pattern": _V1_LABEL_PATTERN},
                    "model": {"type": "string", "pattern": _V1_MODEL_PATTERN},
                    "rates_microunits": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": list(_RATE_KEYS),
                        "properties": {
                            key: {"type": "integer", "minimum": 0} for key in _RATE_KEYS
                        },
                    },
                },
            },
        },
    },
}


def build_usage_pricing_schema() -> dict[str, object]:
    """Return the frozen schema-v1 contract used by runtime and documentation."""
    return deepcopy(_PRICE_BOOK_SCHEMA_V1)


class UsageCostEstimator(Protocol):
    @property
    def fingerprint(self) -> str: ...

    def price(
        self,
        identity: UsageIdentity,
        amount: UsageAmount,
        *,
        phase: Literal["reserve", "settle"],
    ) -> UsageAmount: ...


@dataclass(frozen=True)
class _Rates:
    request: int
    million_input_tokens: int
    million_output_tokens: int
    embedding_item: int
    rerank_document: int
    image: int


class UsagePriceBook:
    """An immutable validated price book with no plugin-visible provenance."""

    def __init__(
        self,
        rules: Mapping[tuple[ProviderCapability, str, str | None], _Rates],
        fingerprint: str,
    ) -> None:
        self._rules = dict(rules)
        self._fingerprint = fingerprint

    def __repr__(self) -> str:
        return f"UsagePriceBook(fingerprint={self.fingerprint!r})"

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    @classmethod
    def load(cls, path: Path) -> UsagePriceBook:
        resolved = Path(path)
        if not resolved.is_file():
            raise ConfigurationError(f"usage price book does not exist: {resolved}")
        try:
            raw = json.loads(resolved.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ConfigurationError(f"usage price book contains invalid JSON: {resolved}: {error}") from error
        except (OSError, UnicodeError) as error:
            raise ConfigurationError(f"failed to read usage price book: {resolved}: {error}") from error

        validator = Draft202012Validator(_PRICE_BOOK_SCHEMA_V1, format_checker=FormatChecker())
        validation_errors = sorted(
            validator.iter_errors(raw),
            key=lambda item: tuple(str(part) for part in item.path),
        )
        if validation_errors:
            validation_error = validation_errors[0]
            location = ".".join(str(part) for part in validation_error.path) or "root"
            raise ConfigurationError(
                f"usage price book {location} [{validation_error.validator}]: {validation_error.message}"
            )
        if not isinstance(raw, dict):
            raise ConfigurationError("usage price book root must be an object")

        canonical = cls._canonical_document(raw)
        rules: dict[tuple[ProviderCapability, str, str | None], _Rates] = {}
        raw_rules = canonical["rules"]
        if not isinstance(raw_rules, list):
            raise ConfigurationError("usage price book rules must be an array")
        for raw_rule in raw_rules:
            if not isinstance(raw_rule, dict):
                raise ConfigurationError("usage price book rule must be an object")
            capability = ProviderCapability(str(raw_rule["capability"]))
            model = str(raw_rule["model"])
            provider = raw_rule.get("provider")
            provider_value = str(provider) if provider is not None else None
            key = (capability, model, provider_value)
            if key in rules:
                provider_label = provider_value if provider_value is not None else "generic"
                raise ConfigurationError(
                    f"duplicate usage price rule for {capability.value}/{model}/{provider_label}"
                )
            rates = raw_rule["rates_microunits"]
            if not isinstance(rates, dict):
                raise ConfigurationError("usage price book rates_microunits must be an object")
            rules[key] = _Rates(**{rate_key: int(rates[rate_key]) for rate_key in _RATE_KEYS})

        encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return cls(rules, hashlib.sha256(encoded).hexdigest())

    @staticmethod
    def _canonical_document(raw: dict[str, object]) -> dict[str, object]:
        effective_date = str(raw["effective_date"])
        try:
            date.fromisoformat(effective_date)
        except ValueError as error:
            raise ConfigurationError("usage price book effective_date must be a valid YYYY-MM-DD date") from error
        source_urls = raw.get("source_urls", [])
        if not isinstance(source_urls, list):
            raise ConfigurationError("usage price book source_urls must be an array")
        for source_url in source_urls:
            try:
                parsed = urlsplit(str(source_url))
                hostname = parsed.hostname
            except ValueError as error:
                raise ConfigurationError("usage price book source_urls must contain valid HTTPS URLs") from error
            if parsed.scheme != "https" or hostname is None:
                raise ConfigurationError("usage price book source_urls must contain valid HTTPS URLs")
        return {
            "schema_version": 1,
            "currency": "CNY",
            "effective_date": effective_date,
            "source_urls": list(source_urls),
            "rules": raw["rules"],
        }

    @staticmethod
    def _million_rate(amount: int, rate: int) -> int:
        if amount == 0 or rate == 0:
            return 0
        return (amount * rate + 999_999) // 1_000_000

    def price(
        self,
        identity: UsageIdentity,
        amount: UsageAmount,
        *,
        phase: Literal["reserve", "settle"],
    ) -> UsageAmount:
        if phase not in {"reserve", "settle"}:
            raise ValueError("pricing phase must be 'reserve' or 'settle'")
        rates = self._rules.get((identity.capability, identity.model, identity.provider))
        if rates is None:
            rates = self._rules.get((identity.capability, identity.model, None))
        if rates is None:
            return replace(amount, cost_microunits=None)
        if any(
            getattr(rates, rate_name) > 0 and unit_name in amount.unknown_units
            for rate_name, unit_name in _RATE_TO_UNIT.items()
        ):
            return replace(amount, cost_microunits=None)
        cost = (
            amount.requests * rates.request
            + self._million_rate(amount.input_tokens, rates.million_input_tokens)
            + self._million_rate(amount.output_tokens, rates.million_output_tokens)
            + amount.embedding_items * rates.embedding_item
            + amount.rerank_documents * rates.rerank_document
            + amount.images * rates.image
        )
        return replace(amount, cost_microunits=cost)


__all__ = ["UsageCostEstimator", "UsagePriceBook", "build_usage_pricing_schema"]
