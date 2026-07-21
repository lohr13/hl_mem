from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def expire_claims(connection: sqlite3.Connection, now: str | None = None) -> dict[str, int]:
    """Expire active ephemeral claims whose TTL is strictly in the past."""
    reference = now or datetime.now(timezone.utc).isoformat()
    cursor = connection.execute(
        "UPDATE claims SET status='expired',valid_to=CASE "
        "WHEN valid_to IS NULL OR expires_at<valid_to THEN expires_at ELSE valid_to END "
        "WHERE status='active' "
        "AND volatility='ephemeral' AND expires_at IS NOT NULL AND expires_at<?",
        (reference,),
    )
    connection.commit()
    return {"expired": cursor.rowcount}
