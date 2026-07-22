"""向后兼容 re-export；实现已迁移到 :mod:`hl_mem.application.ingest`。"""

from hl_mem.application.ingest import IngestService, claim_text, compute_fact_hash, new_id

store_extracted = IngestService.store_extracted


def _build_observation(*_args, **_kwargs) -> None:
    """已废弃 — 观察构建逻辑已移除。保留为 no-op 以兼容 monkeypatch。"""
    return None


__all__ = ["IngestService", "claim_text", "compute_fact_hash", "new_id", "store_extracted"]
