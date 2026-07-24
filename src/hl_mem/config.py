"""领域与算法常量。部署相关配置统一由 :mod:`hl_mem.settings` 提供。"""

from __future__ import annotations

# 可运行时调优的策略值属于 Settings；本模块只保留领域与纯算法常量。

# 去重 / 冲突阈值
DEDUP_SEMANTIC_THRESHOLD = 0.82
CONSOLIDATE_GRAY_ZONE_MIN = 0.72
CONSOLIDATE_GRAY_ZONE_MAX = 0.95

# Worker 调度
WORKER_MAINTENANCE_INTERVAL = 600.0
WORKER_JOB_LEASE_MINUTES = 5
WORKER_POLL_INTERVAL = 2.0

# 召回
RECALL_DEFAULT_LIMIT = 20
RECALL_VECTOR_SCAN_LIMIT = 200

# 数据保留
RETENTION_DAYS = 30

# canonical attribute TTL。仅显式列出的短期状态设置额外到期时间，其余 active claim 不受影响。
STATE_TRANSIENT_TTL_DAYS = 7
STATE_TEST_SUITE_TTL_DAYS = 7
ATTRIBUTE_TTL_DAYS: dict[str, int] = {
    "state.service_health": STATE_TRANSIENT_TTL_DAYS,
    "state.process": STATE_TRANSIENT_TTL_DAYS,
    "state.connectivity": STATE_TRANSIENT_TTL_DAYS,
    "state.test_suite": STATE_TEST_SUITE_TTL_DAYS,
}
