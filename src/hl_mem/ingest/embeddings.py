"""已弃用的 embedding 组件兼容入口。"""

import warnings

warnings.warn(
    "hl_mem.ingest.embeddings is deprecated and will be removed in v0.6.0; "
    "use hl_mem.ingest.embedder instead",
    DeprecationWarning,
    stacklevel=2,
)

from hl_mem.ingest.embedder import *  # noqa: F403
