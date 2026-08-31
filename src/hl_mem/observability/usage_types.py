"""Validated value objects shared by Provider usage governance."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from hl_mem.plugins.contracts import ProviderCapability

_COUNTER_FIELDS = (
    "requests",
    "input_tokens",
    "output_tokens",
    "embedding_items",
    "rerank_documents",
    "images",
)
_LABEL_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?")
_MODEL_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._:/-]{0,198}[A-Za-z0-9])?")


def _require_integer(name: str, value: int, *, non_negative: bool = False) -> None:
    if type(value) is not int or (non_negative and value < 0):
        qualifier = "non-negative integer" if non_negative else "integer"
        raise ValueError(f"{name} must be a {qualifier}")


@dataclass(frozen=True)
class UsageAmount:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    embedding_items: int = 0
    rerank_documents: int = 0
    images: int = 0
    cost_microunits: int | None = None
    unknown_units: frozenset[str] = field(default_factory=frozenset, repr=False)

    def __post_init__(self) -> None:
        for field_name in _COUNTER_FIELDS:
            _require_integer(field_name, getattr(self, field_name), non_negative=True)
        if self.cost_microunits is not None:
            _require_integer("cost_microunits", self.cost_microunits, non_negative=True)
        if not isinstance(self.unknown_units, frozenset) or not self.unknown_units.issubset(_COUNTER_FIELDS):
            raise ValueError("unknown_units must be a frozenset of usage counter names")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: object) -> UsageAmount:
        if not isinstance(other, UsageAmount):
            return NotImplemented
        cost = (
            self.cost_microunits + other.cost_microunits
            if self.cost_microunits is not None and other.cost_microunits is not None
            else None
        )
        return UsageAmount(
            requests=self.requests + other.requests,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            embedding_items=self.embedding_items + other.embedding_items,
            rerank_documents=self.rerank_documents + other.rerank_documents,
            images=self.images + other.images,
            cost_microunits=cost,
            unknown_units=self.unknown_units | other.unknown_units,
        )

    def scale(self, factor: int) -> UsageAmount:
        _require_integer("factor", factor, non_negative=True)
        return UsageAmount(
            requests=self.requests * factor,
            input_tokens=self.input_tokens * factor,
            output_tokens=self.output_tokens * factor,
            embedding_items=self.embedding_items * factor,
            rerank_documents=self.rerank_documents * factor,
            images=self.images * factor,
            cost_microunits=None if self.cost_microunits is None else self.cost_microunits * factor,
            unknown_units=self.unknown_units,
        )


@dataclass(frozen=True)
class UsageLimits:
    daily_requests: int = 0
    daily_tokens: int = 0
    daily_cost_microunits: int = 0

    def __post_init__(self) -> None:
        _require_integer("daily_requests", self.daily_requests)
        _require_integer("daily_tokens", self.daily_tokens)
        _require_integer("daily_cost_microunits", self.daily_cost_microunits)


@dataclass(frozen=True)
class UsageIdentity:
    capability: ProviderCapability
    operation: str
    plugin_id: str
    provider: str
    model: str

    def __post_init__(self) -> None:
        for field_name in ("operation", "plugin_id", "provider"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or _LABEL_PATTERN.fullmatch(value) is None:
                raise ValueError(f"usage identity {field_name} must be a bounded low-cardinality label")
        if not isinstance(self.model, str) or _MODEL_PATTERN.fullmatch(self.model) is None:
            raise ValueError("usage identity model must be a bounded model identifier")


@dataclass(frozen=True)
class UsageReservation:
    id: str
    reserved: UsageAmount
    lease_expires_at: datetime


def default_usage_ledger_path(database_path: str | Path) -> Path:
    return Path(database_path).with_suffix(".budget.db")
