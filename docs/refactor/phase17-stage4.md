# Phase 17 Stage 4: TTL 三因子 + importance 治理 → v0.9.0

## 目标

1. 统一 TTL 策略到 retention 纯函数
2. importance 联动 TTL（低 importance 快过期）
3. 存量数据 expires_at 回填

## 前提

Stage 1-3 已完成（v0.8.1）：
- canonical_slot 生效
- TTL worker 已移除 volatility 条件（只看 expires_at）
- 但 expires_at 的计算仍散在多处

## 改动清单

### 1. 新建 retention 纯函数模块

文件：domain/claims/retention.py（新建）

```python
def compute_expiration(
    scope: str,
    importance: float,
    volatility: str,
    canonical_slot: str | None,
    valid_to: str | None,
    observed_at: str,
    recorded_from: str,
    policy: TTLPolicy,
) -> tuple[str | None, str]:
    """计算 expires_at 和 reason code。

    返回 (expires_at_iso, reason)

    策略：
    1. permanent → None（永不过期，除非 valid_to）
    2. temporal:
       - importance < 0.4 → 3 天 TTL
       - importance 0.4-0.7 → 7 天 TTL
       - importance > 0.7 → 14 天 TTL
    3. valid_to 存在 → expires_at = min(computed, valid_to)
    4. volatile slot (state.service_health) → max 1 天 TTL

    reason codes: "permanent", "temporal_low", "temporal_normal", "temporal_high",
                  "slot_short", "valid_to_override", "none"
    """
```

### 2. TTL 矩阵从 Settings 读取

文件：settings.py

```python
# TTL 配置
temporal_ttl_days_low: int = 3       # importance < 0.4
temporal_ttl_days_normal: int = 7    # importance 0.4-0.7
temporal_ttl_days_high: int = 14     # importance > 0.7
importance_low_threshold: float = 0.4
importance_high_threshold: float = 0.7
importance_write_floor: float = 0.2  # 低于此值不写入
slot_short_ttl_seconds: int = 86400  # state.service_health 等
```

### 3. 写入时调用 retention 函数

文件：application/ingest.py — _build_claim_drafts()

- 调用 compute_expiration() 计算 expires_at
- importance < importance_write_floor → 不写入，返回明确结果（不静默丢弃）
- 替换当前散落的 TTL 计算逻辑

### 4. reclassify 时重算 expires_at

文件：workers/reclassify.py — reclassify_claims()

- scope/importance 变化时调用 compute_expiration() 重算
- 从原始锚点（observed_at/recorded_from）重算，不从旧 expires_at 增量更新
- expired claim 不因 importance 提升复活

### 5. 存量回填

文件：workers/ttl.py 或新建 workers/backfill_expires_at.py

- 对所有 active 的 temporal claims 重算 expires_at
- importance < 0.4 且 age > 3 天的立即 expire
- 支持 dry-run + 分批 + grace period
- 永久保护类型（memory.explicit, identity.name）跳过

### 6. importance 打分指南写入 prompt

文件：ingest/llm_extractor.py — SYSTEM_PROMPT

```
importance 打分指南：
- 0.9-1.0：核心身份、永久偏好、关键约束
- 0.7-0.8：重要架构决策、工具选择、配置
- 0.5-0.6：项目状态、计划、一般事实
- 0.3-0.4：一次性操作记录、临时状态
- < 0.2：不写入（噪声）

保护类型（即使低分也写入）：
- explicit_memory
- identity.name
```

## 约束

- 不要修改 tests/
- 不要运行 pytest
- git add src/ && git commit
- 不要用 git add -A
- 版本 bump 0.8.1 → 0.9.0
- retention 函数是纯函数，不依赖 DB 或 LLM
- 从原始时间锚点重算，不增量更新
- expired 不复活
