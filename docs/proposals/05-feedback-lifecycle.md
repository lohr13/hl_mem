# 反馈驱动的派生记忆维护实现方案

## 目标与原则

把 retrieval feedback 从排序信号扩展为有界的 retention/decay 信号，并分别维护 claim、observation、policy 的 usefulness。严格区分：

- truth confidence：事实或派生结论有多可信；
- usefulness：它在任务中是否有帮助；
- policy reliability：过程策略在成功执行中的可靠程度。

“未帮助”只降低 usefulness，不降低 Claim truth confidence。显式 correction 不走负分捷径，而进入已有冲突、撤回或 supersede 路径。

## 现状梳理

- migration 008 的 `retrieval_feedback` 包含 `query_id,memory_type,memory_id,rank,score,used_by_model,helpful,task_outcome,created_at`。
- `application/recall.py::RecallService._record_feedback()` 在返回时批量写 exposure，helpful/task_outcome 初始为空。
- `storage/experience.py::submit_retrieval_feedback()` 更新 claim feedback；当前接口以 query_id + memory_id 定位。
- `storage/claims.py::ClaimRepository.helpful_rates()` 对 claim 直接 `avg(helpful)`。
- `recall/staged_pipeline.py::_filter_and_score()` 把 helpful_rate 注入 `ranking.memory_features()`；无反馈通常回落 0.5。
- `workers/decay.py::decay_claims()` 目前用 scope、inactive days、access_count bonus 调整 decay/archive 边界，并线性降低 confidence。
- `derivations` 有 confidence/status/proof_count，但没有独立 usefulness；`policies` 有 reliability/success/failure，不能与通用 helpful 混用。

问题是稀疏反馈直接平均易受单票影响，access_count 只表示“被返回”，且 decay 当前直接改 truth confidence。

## 类型与服务边界

在 `src/hl_mem/protocols.py` 增加：

```python
from dataclasses import dataclass
from typing import Literal, Protocol

MemoryType = Literal["claim", "observation", "policy"]


@dataclass(frozen=True)
class UsefulnessSnapshot:
    """基于反馈聚合的有界 usefulness 状态。"""

    memory_type: MemoryType
    memory_id: str
    helpful_count: int
    unhelpful_count: int
    success_sum: float
    outcome_count: int
    usefulness_score: float
    retention_bonus_days: int
    updated_at: str


class UsefulnessPolicyProtocol(Protocol):
    """从反馈计数计算 usefulness 与 retention bonus。"""

    def evaluate(
        self,
        *,
        helpful_count: int,
        unhelpful_count: int,
        success_sum: float,
        outcome_count: int,
    ) -> tuple[float, int]: ...
```

`domain/feedback.py::BayesianUsefulnessPolicy` 是纯函数。建议：

```text
helpful_rate = (helpful_count + 2) / (helpful_count + unhelpful_count + 4)
success_rate = (success_sum + 1) / (outcome_count + 2)
usefulness = 0.7 * helpful_rate + 0.3 * success_rate
positive_evidence = helpful_count + floor(success_sum)
bonus_days = min(MAX_BONUS_DAYS,
                 floor(positive_evidence / BONUS_EVERY) * BONUS_DAYS)
```

默认 `BONUS_EVERY=3`、`BONUS_DAYS=14`、`MAX_BONUS_DAYS=180`，由 Settings 注入。负反馈能把 usefulness 拉回，但 bonus 只由正向证据产生且受 cap 限制；过期短 slot 和显式 valid_to 不得被 bonus 越过。

## Migration 024：usefulness 聚合

完整 DDL：

```sql
CREATE TABLE IF NOT EXISTS memory_usefulness (
    memory_type TEXT NOT NULL
        CHECK (memory_type IN ('claim','observation','policy')),
    memory_id TEXT NOT NULL,
    helpful_count INTEGER NOT NULL DEFAULT 0 CHECK (helpful_count >= 0),
    unhelpful_count INTEGER NOT NULL DEFAULT 0 CHECK (unhelpful_count >= 0),
    success_sum REAL NOT NULL DEFAULT 0.0,
    outcome_count INTEGER NOT NULL DEFAULT 0 CHECK (outcome_count >= 0),
    usefulness_score REAL NOT NULL DEFAULT 0.5
        CHECK (usefulness_score >= 0.0 AND usefulness_score <= 1.0),
    retention_bonus_days INTEGER NOT NULL DEFAULT 0
        CHECK (retention_bonus_days >= 0),
    last_positive_at TEXT,
    last_negative_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (memory_type, memory_id)
);

CREATE INDEX IF NOT EXISTS idx_memory_usefulness_score
ON memory_usefulness(memory_type, usefulness_score DESC, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_retrieval_feedback_memory_created
ON retrieval_feedback(memory_type, memory_id, created_at);

CREATE INDEX IF NOT EXISTS idx_retrieval_feedback_query_memory
ON retrieval_feedback(query_id, memory_type, memory_id);
```

不对多态 memory_id 建伪 FK；repository 更新时显式验证对应 claims/derivations/policies 存在。migration 文件为 `024_memory_usefulness.sql`。部署后 backfill 从已有非空 feedback 聚合；backfill 是幂等 Python migration helper 或 worker，不在 DDL 中依赖复杂版本差异。

## 反馈写入与聚合

修改 `ExperienceRepository.submit_retrieval_feedback()`，API 必须提交 recall 返回的唯一 `feedback_id`（即 `retrieval_feedback.id`），不能再用 query_id + memory_id 猜最近一条 exposure：

1. `BEGIN IMMEDIATE`；
2. 用主键 `id=?` 精确锁定 exposure；拒绝 helpful 非 0/1、outcome 不在 `[0,1]`；
3. 若该 exposure 已有相同反馈，幂等返回；若要改票，先减旧聚合再加新聚合；
4. UPSERT `memory_usefulness` 计数；
5. 调用纯 policy 重算 score/bonus；
6. commit。

新增 batch worker `rebuild_usefulness` 用于审计修复，始终以 `retrieval_feedback` 为 source of truth。`ClaimRepository.helpful_rates()` 改为批量 LEFT JOIN `memory_usefulness`；少于最小反馈数仍返回贝叶斯平滑值，不直接 avg。

observation 对应 `derivations.kind='observation'`；policy 对应 `policies`。Recall packed context 记录这三类 exposure 时统一写 retrieval_feedback，不能只给 claims 写。各类型更新各自 usefulness 行：

- observation：影响派生记忆召回排序、重建优先级和 archive 时机，不改 `derivations.confidence`；
- policy：影响 procedure intent 排序和退休候选；`policies.reliability` 仍只由 episode execution outcome 更新；
- claim：影响排序、TTL bonus 和 decay schedule，不改事实校验逻辑。

## Retention bonus 与衰减

### TTL

对有 `expires_at` 的 temporal Claim，正反馈可延长基础 TTL，但需保留硬上限：

```text
effective_expires_at =
min(base_expires_at + retention_bonus_days,
    valid_to if present,
    observed_at + scope_max_ttl_days,
    observed_at + slot_hard_cap_days if short slot)
```

不反复 UPDATE 基础 `expires_at`，避免 bonus 撤销困难；`workers/ttl.py::expire_claims()` 查询时 LEFT JOIN usefulness 计算 effective boundary。若性能需要，可在 worker 批次内用 Python 纯函数计算。permanent Claim 仍无 TTL。

### Decay

当前 `workers/decay.py` 会降低 Claim truth confidence，因此任何包含负反馈的 factor 都不能改变 confidence 衰减时间，否则“未帮助”会间接降低真实性。本特性只允许正向 retention bonus 延后 decay/archive：

```text
effective_decay_window = decay_after + retention_bonus_days
effective_archive_window = archive_after + retention_bonus_days
```

unhelpful 只改变 usefulness 排序和派生记忆维护优先级，对 Claim 的 decay/archive 时间为零影响；正反馈延后窗口并受 cap 和 rollout grace 限制。若未来希望低 usefulness 加速清理，必须先用独立 migration 拆出 `decay_score`，并证明它不写 `claims.confidence`，不属于本方案范围。

observation/policy 不走 Claim TTL worker：新增 maintenance 查询，低 usefulness 且长期无正反馈时分别标记 derivation `archived`、policy `retired`，仍须满足现有 proof/support 和 lifecycle guard，单次负反馈不能触发。

## 显式 correction

API/MCP feedback DTO 增加可选：

```python
@dataclass(frozen=True)
class ExplicitCorrection:
    memory_type: Literal["claim"]
    memory_id: str
    corrected_text: str | None
    action: Literal["retract", "replace"]
    idempotency_key: str
```

- `retract`：创建 `event_type='feedback'` 原始证据，调用 `ForgetService`/lifecycle guard 将目标 Claim retracted，并 stale 传播。
- `replace`：创建 correction event，走正常 extractor/store pipeline；相同 canonical attribute 触发 conflict/state_change/supersede，灰区进入 conflict case。
- correction 自身也可提交 helpful，但 correction action 不能由 `helpful=0` 隐式推断。
- 只有用户显式动作或授权 API 字段能触发，模型推断不得自动撤回。

## 配置与文件变更

```text
HL_MEM_FEEDBACK_LIFECYCLE_MODE=off|observe|on   # 默认 observe
HL_MEM_FEEDBACK_BONUS_EVERY=3
HL_MEM_FEEDBACK_BONUS_DAYS=14
HL_MEM_FEEDBACK_BONUS_CAP_DAYS=180
HL_MEM_FEEDBACK_MIN_SAMPLES=3
```

- 新建 `domain/feedback.py`、migration 024、可选 `workers/rebuild_usefulness.py`。
- 修改 `storage/experience.py`、`storage/claims.py`、`experience/service.py`。
- 修改 `application/recall.py` 记录 observation/policy exposure。
- 修改 `workers/ttl.py`、`workers/decay.py` 和派生/策略 maintenance。
- 修改 API/MCP schemas 与 feedback endpoint 支持 explicit correction。

## 测试计划

- 现状回归：无反馈时 helpful prior、排序与 lifecycle 等价。
- 聚合：正/负/改票/重复提交、batch rebuild 一致、并发事务、非法 memory ID/type。
- 公式：贝叶斯平滑、bonus 阶梯/cap、feedback factor min/max。
- truth/usefulness 隔离：unhelpful 不改变 Claim confidence/status；observation usefulness 不改 derivation confidence；policy usefulness 不改 reliability。
- TTL：正反馈延长、valid_to 和 short-slot hard cap 优先、permanent 不受影响。
- decay：正向 bonus 延后；任意数量 unhelpful 都不改变 Claim confidence/decay/archive 时间；每天幂等。
- correction：retract stale 传播；replace 走 conflict/supersede；无 idempotency key 拒绝。
- migration/backfill：从 023/022 升级、重复 backfill、索引查询计划。

## 验收标准

- usefulness 可按三类 memory 独立查询和重建。
- 有用反馈只提供有界保留奖励；“未帮助”永不被解释为“不真实”。
- correction 全程有 event 和 conflict/lifecycle 审计链。
