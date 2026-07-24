"""已弃用的 canonical attribute 兼容入口。"""

import warnings

warnings.warn(
    "hl_mem.recall.attribute_map is deprecated and will be removed in v0.6.0; "
    "use hl_mem.domain.claims.attributes instead",
    DeprecationWarning,
    stacklevel=2,
)

from hl_mem.domain.claims.attributes import *  # noqa: F403
