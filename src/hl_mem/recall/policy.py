"""已弃用的召回策略兼容入口。"""

import warnings

warnings.warn(
    "hl_mem.recall.policy is deprecated and will be removed in v0.6.0; "
    "use hl_mem.domain.recall instead",
    DeprecationWarning,
    stacklevel=2,
)

from hl_mem.domain.recall import *  # noqa: F403
