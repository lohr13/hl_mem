"""Bounded source-locator projections safe for diagnostics and model context."""

from __future__ import annotations

from urllib.parse import urlsplit

_MAX_SOURCE_URI_LENGTH = 2048


def safe_source_hint(source_uri: object) -> str | None:
    """Reduce a locator to a scheme and host without credentials, path, or query."""
    if not isinstance(source_uri, str) or not source_uri or len(source_uri) > _MAX_SOURCE_URI_LENGTH:
        return None
    try:
        parsed = urlsplit(source_uri)
        host = parsed.hostname
        if parsed.scheme in {"http", "https"} and host:
            safe_host = f"[{host}]" if ":" in host else host
            return f"{parsed.scheme}://{safe_host}"[:255]
        if parsed.scheme == "file":
            return "file"
    except (ValueError, UnicodeError):
        return None
    return None


__all__ = ["safe_source_hint"]
