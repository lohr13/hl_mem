#!/usr/bin/env python3
"""Cross-platform health probe for an HL-Mem service."""

from __future__ import annotations

import argparse
import json
import sys
from http.client import HTTPException
from urllib.request import urlopen

DEFAULT_URL = "http://127.0.0.1:8200/healthz"


def probe(url: str, timeout: float) -> tuple[bool, str]:
    try:
        with urlopen(url, timeout=timeout) as response:  # noqa: S310
            status_code = response.getcode()
            payload = json.loads(response.read())
        if status_code != 200:
            return False, f"HTTP {status_code}"
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            return False, f"unexpected status={payload.get('status')!r}" if isinstance(payload, dict) else "invalid body"
        return True, "status=ok"
    except (OSError, ValueError, HTTPException) as exc:
        return False, str(exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--timeout", type=float, default=5)
    args = parser.parse_args(argv)
    healthy, reason = probe(args.url, args.timeout)
    if sys.stdout is not None:
        sys.stdout.write(f"{'ok' if healthy else 'fail'}: {reason}\n")
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
