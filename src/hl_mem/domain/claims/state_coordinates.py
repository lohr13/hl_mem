"""不可变状态坐标值对象。"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TypeAlias

FrozenQualifierValue: TypeAlias = str


def _validated_identifier(field: str, value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    if not value.strip():
        raise ValueError(f"{field} must not be blank")
    return value


def _json_compatible(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("coordinate qualifier numbers must be finite")
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("coordinate qualifier key must be a non-blank string")
            result[key] = _json_compatible(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_json_compatible(item) for item in value]
    raise TypeError("coordinate qualifier values must be JSON-compatible")


def _freeze_qualifier_value(value: Any) -> FrozenQualifierValue:
    return json.dumps(
        _json_compatible(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _freeze_qualifiers(values: Mapping[str, Any]) -> tuple[tuple[str, FrozenQualifierValue], ...]:
    items: list[tuple[str, FrozenQualifierValue]] = []
    for key, value in values.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("coordinate qualifier key must be a non-blank string")
        items.append((key, _freeze_qualifier_value(value)))
    return tuple(sorted(items, key=lambda item: item[0]))


@dataclass(frozen=True, slots=True, init=False)
class StateCoordinate:
    """由 namespace、主体、slot 与坐标 qualifier 唯一标识的状态轴。"""

    namespace: str
    canonical_subject: str
    canonical_slot: str
    coordinate_qualifiers: tuple[tuple[str, FrozenQualifierValue], ...]

    def __init__(
        self,
        namespace: str,
        canonical_subject: str,
        canonical_slot: str,
        coordinate_qualifiers: Mapping[str, Any] | None = None,
    ) -> None:
        object.__setattr__(self, "namespace", _validated_identifier("namespace", namespace))
        object.__setattr__(
            self,
            "canonical_subject",
            _validated_identifier("canonical_subject", canonical_subject),
        )
        object.__setattr__(
            self,
            "canonical_slot",
            _validated_identifier("canonical_slot", canonical_slot),
        )
        if coordinate_qualifiers is None:
            coordinate_qualifiers = {}
        elif not isinstance(coordinate_qualifiers, Mapping):
            raise TypeError("coordinate_qualifiers must be a mapping")
        object.__setattr__(
            self,
            "coordinate_qualifiers",
            _freeze_qualifiers(coordinate_qualifiers),
        )
