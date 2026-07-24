"""已弃用的写入管线兼容入口。"""

import warnings

warnings.warn(
    "hl_mem.api.pipeline is deprecated and will be removed in v0.6.0; "
    "use hl_mem.application.ingest instead",
    DeprecationWarning,
    stacklevel=2,
)

from hl_mem.application.ingest import IngestService, claim_text, compute_fact_hash, new_id

store_extracted = IngestService.store_extracted


__all__ = ["IngestService", "claim_text", "compute_fact_hash", "new_id", "store_extracted"]
