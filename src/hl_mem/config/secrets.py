"""Secret loading and redaction at the configuration boundary."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path

from hl_mem.errors import ConfigurationError


def is_placeholder_secret(value: str | None) -> bool:
    """Return whether a secret is absent or still uses a known placeholder."""
    if value is None:
        return True
    normalized = value.strip().lower()
    if not normalized:
        return True
    if normalized in {"xxx", "your-key", "your_key", "changeme", "change-me"}:
        return True
    if normalized.startswith("<") and normalized.endswith(">"):
        return True
    return bool(re.fullmatch(r"(?:sk-)?x{3,}", normalized)) or (
        normalized.startswith("sk-") and normalized.endswith("xxx")
    )


def read_secret_values(
    path: Path,
    names: Iterable[str],
    environ: Mapping[str, str],
) -> dict[str, str]:
    """Read supported dotenv values, with the process environment taking precedence."""
    supported = frozenset(names)
    values: dict[str, str] = {}
    if path.is_file():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            raise ConfigurationError(f"{path}: failed to read secret file: {error}") from error

        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                continue
            name, raw_value = line.split("=", 1)
            name = name.strip()
            if name not in supported:
                continue
            value = raw_value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            else:
                value = value.split(" #", 1)[0].rstrip()
            values[name] = value

    for name in supported:
        if name in environ:
            values[name] = environ[name]
    return values


def redact_secret_text(text: str, values: Iterable[str | None]) -> str:
    """Replace non-empty secret values in diagnostic text without guessing formats."""
    redacted = text
    for value in sorted({item for item in values if item}, key=len, reverse=True):
        redacted = redacted.replace(value, "<redacted>")
    return redacted


__all__ = ["is_placeholder_secret", "read_secret_values", "redact_secret_text"]
