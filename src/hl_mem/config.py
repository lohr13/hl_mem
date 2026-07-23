"""集中化的配置常量。所有 magic number 都在此定义，可通过环境变量覆盖。"""

from __future__ import annotations

import os

# 去重 / 冲突阈值
DEDUP_SEMANTIC_THRESHOLD = float(os.getenv("HL_MEM_DEDUP_THRESHOLD", "0.82"))
CONSOLIDATE_GRAY_ZONE_MIN = float(os.getenv("HL_MEM_CONSOLIDATE_GRAY_MIN", "0.72"))
CONSOLIDATE_GRAY_ZONE_MAX = float(os.getenv("HL_MEM_CONSOLIDATE_GRAY_MAX", "0.95"))

# Worker 调度
WORKER_MAINTENANCE_INTERVAL = float(os.getenv("HL_MEM_WORKER_MAINTENANCE_INTERVAL", "600"))
WORKER_JOB_LEASE_MINUTES = int(os.getenv("HL_MEM_WORKER_LEASE_MINUTES", "5"))
WORKER_POLL_INTERVAL = float(os.getenv("HL_MEM_WORKER_POLL_INTERVAL", "2.0"))

# 召回
RECALL_DEFAULT_LIMIT = int(os.getenv("HL_MEM_RECALL_DEFAULT_LIMIT", "20"))
RECALL_VECTOR_SCAN_LIMIT = int(os.getenv("HL_MEM_RECALL_VECTOR_SCAN_LIMIT", "200"))

# 数据保留
RETENTION_DAYS = int(os.getenv("HL_MEM_RETENTION_DAYS", "30"))
