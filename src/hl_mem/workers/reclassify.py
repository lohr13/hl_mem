from __future__ import annotations

import argparse
import json
import os
from typing import Any, Iterable

from hl_mem import components
from hl_mem.ingest.llm_extractor import LLMExtractor
from hl_mem.storage.database import Database


CLASSIFY_PROMPT = """Classify each supplied memory without extracting or rewriting it.
Return JSON {"classifications":[{"id":...,"scope":"temporal|permanent","importance":0.0-1.0}]}.
Scope is independent from volatility: temporal is useful for a bounded real-world period;
permanent is a durable preference, identity, convention, configuration, or long-term memory.
Importance: 0.0-0.3 incidental, 0.4-0.6 useful, 0.7-0.9 important, 1.0 must remember.
Do not infer importance merely from emotional wording."""


def _chunks(values: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _text(claim: dict[str, Any]) -> str:
    value = claim.get("value_json")
    try:
        value = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError:
        pass
    return f"{claim.get('subject_entity_id') or ''} {claim.get('predicate') or ''} {value or ''}".strip()


def classify_batch(extractor: LLMExtractor, claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload = {"model": extractor.model, "messages": [
        {"role": "system", "content": CLASSIFY_PROMPT},
        {"role": "user", "content": json.dumps(
            [{"id": claim["id"], "text": _text(claim)} for claim in claims], ensure_ascii=False)},
    ], "response_format": {"type": "json_object"}}
    response = extractor._post(payload)
    parsed = extractor._parse_json(response["choices"][0]["message"]["content"])
    values = parsed.get("classifications", [])
    return values if isinstance(values, list) else []


def reclassify_claims(connection: Any, extractor: LLMExtractor, batch_size: int = 8) -> dict[str, int]:
    if not 5 <= batch_size <= 10:
        raise ValueError("batch_size must be between 5 and 10")
    rows = [dict(row) for row in connection.execute("SELECT * FROM claims ORDER BY id").fetchall()]
    pending = [row for row in rows
               if row.get("scope", "permanent") == "permanent"
               and float(row.get("importance", 0.5)) == 0.5]
    updated = 0
    for batch in _chunks(pending, batch_size):
        allowed_ids = {claim["id"] for claim in batch}
        for item in classify_batch(extractor, batch):
            claim_id = item.get("id")
            if claim_id not in allowed_ids:
                continue
            scope = item.get("scope", "permanent")
            scope = scope if scope in {"temporal", "permanent"} else "permanent"
            try:
                importance = min(1.0, max(0.0, float(item.get("importance", 0.5))))
            except (TypeError, ValueError):
                importance = 0.5
            updated += connection.execute(
                "UPDATE claims SET scope=?,importance=? WHERE id=?",
                (scope, importance, claim_id),
            ).rowcount
        connection.commit()
    return {"scanned": len(rows), "eligible": len(pending), "updated": updated}


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m hl_mem.workers.reclassify")
    parser.add_argument("--db", default=os.getenv("HL_MEM_DB_PATH", "hl_mem.db"))
    parser.add_argument("--batch-size", type=int, default=8)
    args = parser.parse_args()
    database = Database(args.db)
    try:
        try:
            extractor = components.make_extractor({"extractor_name": "real", "require_real": True})
        except RuntimeError as error:
            raise SystemExit("LLM_API_KEY is required") from error
        print(json.dumps(reclassify_claims(database.open(), extractor, args.batch_size),
                         ensure_ascii=False, sort_keys=True))
    finally:
        database.close()


if __name__ == "__main__":
    main()
