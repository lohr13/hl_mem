"""Domain-oriented HTTP route registration."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_namespace_alias(namespace: str | None, tenant_id: str | None) -> str:
    """Resolve the deprecated tenant alias without allowing disagreement."""
    if namespace is not None and tenant_id is not None and namespace != tenant_id:
        raise HTTPException(422, "namespace and deprecated tenant_id must match")
    return namespace or tenant_id or "default"
