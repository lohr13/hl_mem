"""已弃用的 Claim 冲突逻辑兼容入口。"""

import warnings

warnings.warn(
    "hl_mem.recall.conflict is deprecated and will be removed in v0.6.0; "
    "use hl_mem.domain.claims.conflicts instead",
    DeprecationWarning,
    stacklevel=2,
)

from hl_mem.domain.claims.conflicts import *  # noqa: F403
