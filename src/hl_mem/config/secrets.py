"""Secret loading and redaction at the configuration boundary."""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path

from hl_mem.errors import ConfigurationError

SUPPORTED_SECRET_NAMES = frozenset(
    {
        "EMBEDDING_API_KEY",
        "IMAGE_API_KEY",
        "LLM_API_KEY",
        "QUERY_EXPANSION_API_KEY",
        "RERANKER_API_KEY",
    }
)


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


def _render_secret_assignment(name: str, value: str) -> str:
    if name not in SUPPORTED_SECRET_NAMES:
        raise ConfigurationError(f"unsupported secret name: {name}")
    if not value or value != value.strip() or "\n" in value or "\r" in value or " #" in value:
        raise ConfigurationError(f"{name}: secret must be a non-empty single-line value without surrounding spaces")
    return f"{name}={value}"


def _write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


def merge_secret_file(path: Path, updates: Mapping[str, str], *, force: bool = False) -> None:
    """Atomically update supported assignments while preserving unrelated dotenv lines."""
    target = Path(path)
    lines = target.read_text(encoding="utf-8").splitlines() if target.is_file() else []
    rendered = {name: _render_secret_assignment(name, value) for name, value in updates.items()}
    found: set[str] = set()
    merged: list[str] = []
    for line in lines:
        candidate = line.strip()
        if candidate.startswith("export "):
            candidate = candidate[7:].lstrip()
        name = candidate.split("=", 1)[0].strip() if "=" in candidate else ""
        if name not in rendered:
            merged.append(line)
            continue
        if name in found:
            raise ConfigurationError(f"{target}: duplicate secret assignment for {name}")
        found.add(name)
        current = read_secret_values(target, (name,), {})
        if name in current and current[name] != updates[name] and not force:
            raise FileExistsError(f"{target}: {name} already exists; pass --force to replace it")
        merged.append(rendered[name])
    for name in sorted(set(rendered) - found):
        merged.append(rendered[name])
    content = ("\n".join(merged) + "\n").encode("utf-8")
    _write_atomic(target, content)


__all__ = [
    "SUPPORTED_SECRET_NAMES",
    "is_placeholder_secret",
    "merge_secret_file",
    "read_secret_values",
    "redact_secret_text",
]
