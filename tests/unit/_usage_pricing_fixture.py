from __future__ import annotations

import json
from pathlib import Path


def write_usage_price_book(
    path: Path,
    *,
    capability: str,
    provider: str,
    model: str,
    **rates: int,
) -> Path:
    all_rates = {
        "request": 0,
        "million_input_tokens": 0,
        "million_output_tokens": 0,
        "embedding_item": 0,
        "rerank_document": 0,
        "image": 0,
    }
    all_rates.update(rates)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "currency": "CNY",
                "effective_date": "2026-08-31",
                "rules": [
                    {
                        "capability": capability,
                        "provider": provider,
                        "model": model,
                        "rates_microunits": all_rates,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path
