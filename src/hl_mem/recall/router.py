"""已弃用的查询路由兼容入口。"""

from __future__ import annotations

import warnings

warnings.warn(
    "hl_mem.recall.router is deprecated and will be removed in v0.6.0; "
    "use hl_mem.domain.recall instead",
    DeprecationWarning,
    stacklevel=2,
)

from hl_mem.domain.recall import QueryRoute, route_query

__all__ = ["QueryRoute", "route_query"]
