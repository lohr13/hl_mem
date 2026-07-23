"""集中化的配置常量。所有 magic number 都在此定义，可通过环境变量覆盖。"""

from __future__ import annotations

import os

LLM_PROVIDER = os.getenv("HL_MEM_LLM_PROVIDER", "dashscope")
LLM_STRUCTURED_MODE = os.getenv("HL_MEM_LLM_STRUCTURED_MODE", "auto")
LLM_MAX_ATTEMPTS = int(os.getenv("LLM_MAX_ATTEMPTS", "3"))
LLM_SCHEMA_RETRIES = int(os.getenv("HL_MEM_LLM_SCHEMA_RETRIES", "2"))
EXTRACTION_CHUNK_TARGET_CHARS = int(os.getenv("HL_MEM_EXTRACTION_CHUNK_TARGET_CHARS", "12000"))
EXTRACTION_CHUNK_OVERLAP_TURNS = int(os.getenv("HL_MEM_EXTRACTION_CHUNK_OVERLAP_TURNS", "2"))
EXTRACTION_MAX_SPLIT_DEPTH = int(os.getenv("HL_MEM_EXTRACTION_MAX_SPLIT_DEPTH", "3"))

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

# canonical attribute TTL。仅显式列出的短期状态设置额外到期时间，其余 active claim 不受影响。
STATE_TRANSIENT_TTL_DAYS = int(os.getenv("HL_MEM_STATE_TRANSIENT_TTL_DAYS", "7"))
STATE_TEST_SUITE_TTL_DAYS = int(os.getenv("HL_MEM_STATE_TEST_SUITE_TTL_DAYS", "7"))
ATTRIBUTE_TTL_DAYS: dict[str, int] = {
    "state.service_health": STATE_TRANSIENT_TTL_DAYS,
    "state.process": STATE_TRANSIENT_TTL_DAYS,
    "state.connectivity": STATE_TRANSIENT_TTL_DAYS,
    "state.test_suite": STATE_TEST_SUITE_TTL_DAYS,
}
