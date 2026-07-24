"""已弃用的 Claim 去重逻辑兼容入口。"""

import warnings

warnings.warn(
    "hl_mem.recall.dedup is deprecated and will be removed in v0.6.0; "
    "use hl_mem.domain.claims.dedup instead",
    DeprecationWarning,
    stacklevel=2,
)

from hl_mem.domain.claims.dedup import *  # noqa: F403
