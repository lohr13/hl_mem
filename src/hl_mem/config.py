"""领域与算法常量。部署相关配置统一由 :mod:`hl_mem.settings` 提供。"""

from __future__ import annotations

# 可运行时调优的策略值属于 Settings；本模块只保留领域与纯算法常量。

# 写入期、同 subject 候选的 best-match 算法阈值。
DEDUP_SEMANTIC_THRESHOLD = 0.82
