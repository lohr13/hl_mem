"""Cross-group validation for bounded runtime modes."""

from __future__ import annotations

from typing import Protocol

from hl_mem.errors import ConfigurationError


class RuntimeModeSettings(Protocol):
    @property
    def entity_constraint_mode(self) -> str: ...

    @property
    def lesson_signal_mode(self) -> str: ...

    @property
    def memory_disposition_mode(self) -> str: ...

    @property
    def conflict_auto_mode(self) -> str: ...


def validate_runtime_modes(settings: RuntimeModeSettings) -> None:
    """Validate modes whose owners span recall, extraction, and governance."""
    if settings.entity_constraint_mode not in {"off", "observe", "enforce"}:
        raise ConfigurationError("recall.entity_constraint_mode must be 'off', 'observe', or 'enforce'")
    if settings.lesson_signal_mode not in {"off", "observe", "enforce"}:
        raise ConfigurationError("extraction.lesson_signal_mode must be 'off', 'observe', or 'enforce'")
    if settings.memory_disposition_mode not in {"off", "observe", "enforce"}:
        raise ConfigurationError("extraction.memory_disposition_mode must be 'off', 'observe', or 'enforce'")
    if settings.conflict_auto_mode not in {"off", "l0_only"}:
        raise ConfigurationError("conflict.auto_mode must be 'off' or 'l0_only'")
