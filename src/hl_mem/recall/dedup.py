from __future__ import annotations

import json
from typing import Any

from hl_mem.ingest.embeddings import cosine_similarity
from hl_mem.storage.repository import ClaimRepository


class Deduplicator:
    def __init__(self, claim_repo: ClaimRepository, embedder: Any, threshold: float = 0.95) -> None:
        self.claim_repo, self.embedder, self.threshold = claim_repo, embedder, threshold

    def find_duplicate(self, new_claim: dict[str, Any]) -> tuple[str | None, str]:
        candidates = self.claim_repo.find_active(
            new_claim.get("namespace_key", "default"), new_claim.get("subject_entity_id")
        )
        value = self._canonical(new_claim.get("value_json"))
        for claim in candidates:
            if claim.get("conflict_key") == new_claim.get("conflict_key") and self._canonical(
                claim.get("value_json")
            ) == value:
                return claim["id"], "exact"
        blob = new_claim.get("embedding_dense")
        if blob is None:
            blob = self.embedder.embed_one(self._text(new_claim))
            new_claim["embedding_dense"] = blob
        for claim in candidates:
            existing_blob = claim.get("embedding_dense")
            if existing_blob and cosine_similarity(existing_blob, blob) > self.threshold:
                return claim["id"], "semantic"
        return None, "new"

    @staticmethod
    def _canonical(value: Any) -> str:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _text(claim: dict[str, Any]) -> str:
        return f"{claim.get('subject_entity_id', '')} {claim.get('predicate', '')} {claim.get('value_json', '')}"
