# hl_mem 记忆生命周期架构整合任务

## 背景

hl_mem 有完整的记忆生命周期管理（去重→冲突检测→归并→衰减→归档→TTL过期→重分类），但存在三个结构性缺口需要修复。

## 项目位置

`D:/workspace/hl_agent/hl_mem/`

## 测试运行方式

```bash
.venv/Scripts/python.exe -m pytest tests/unit/ -q --tb=short
```

## 三项修改

### 修改 1：新建 `src/hl_mem/workers/lifecycle.py` — 显式状态机

**问题**：Claim 状态转换散落在 5 个文件（consolidate.py / ttl.py / decay.py / repository.py / worker.py），没有统一守卫。disputed → archived 是否合法？expired → active 能否恢复？无定义。

**要求**：

1. 定义 `ClaimStatus` 字符串枚举：`active`, `disputed`, `expired`, `archived`, `superseded`

2. 定义合法转换矩阵（dict 或 frozenset of tuples）：
   ```
   active → disputed      (consolidate 发现矛盾)
   active → expired        (TTL 到期)
   active → archived       (decay 归档)
   active → superseded     (被新 claim 替代)
   disputed → archived     (衰减归档，同 active)
   disputed → expired      (TTL 到期，同 active)
   disputed → active       (人工/归并解决冲突后恢复)
   ```
   不在矩阵中的转换应抛 `InvalidTransitionError`。

3. 提供函数：
   ```python
   def assert_transition(from_status: str, to_status: str) -> None:
       """断言状态转换合法，非法时抛 InvalidTransitionError。"""
   ```

4. 在以下位置集成守卫（读取当前 status → assert → UPDATE）：
   - `workers/consolidate.py` 第 209-210 行：`active → disputed`
   - `workers/ttl.py` 第 10-16 行：`active → expired`（这里是批量 UPDATE，需要先 SELECT 再逐条检查，或者在 WHERE 条件中加 `status='active'` 约束——当前已有，但应调用 assert 确保一致性）
   - `workers/decay.py` 第 58-60 行：`active/disputed → archived`
   - `storage/repository.py` 中的 `supersede_with_inline` 方法：`active → superseded`

5. **向后兼容**：现有测试必须全部通过。状态机是安全网，不应改变现有行为。

### 修改 2：将 `reclassify` 和 `retention` 接入 Worker 调度

**问题**：`workers/reclassify.py` 和 `security/retention.py` 已实现但从未被 worker 调用，是死代码。

**要求**：

#### 2a. 在 `workers/worker.py` 的 `_dispatch` 方法中新增两个 job_type：

```python
if job["job_type"] == "reclassify_claims":
    extractor = self._make_extractor()  # 复用已有的 extractor 创建逻辑
    from hl_mem.workers.reclassify import reclassify_claims
    return reclassify_claims(self.connection, extractor)

if job["job_type"] == "purge_retention":
    from hl_mem.security.retention import purge_retained_events
    # 清理 30 天前无证据依赖的事件
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    return {"purged": purge_retained_events(self.connection, "default", cutoff)}
```

#### 2b. 在 `workers/worker.py` 中新增 `enqueue_daily_reclassify` 函数（参照 `enqueue_daily_consolidation` 的模式）：

```python
def enqueue_daily_reclassify(connection, now: str, cron: str) -> bool:
    """到达计划时间后幂等创建当天的重分类任务。"""
    # 逻辑与 enqueue_daily_consolidation 相同，但 job_type='reclassify_claims'
```

#### 2c. 在 `run_forever` 的定时任务块（第 77-91 行附近）中新增：

```python
enqueue_daily_reclassify(
    self.connection,
    _now(),
    self.config.get("reclassify_cron", os.getenv("HL_MEM_RECLASSIFY_CRON", "04:30")),
)
```

#### 2d. retention 清理不需要每天跑，放在 `run_forever` 的定时块里直接执行（和 expire_claims/decay_claims 一样）：

```python
from hl_mem.security.retention import purge_retained_events
cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
purge_retained_events(self.connection, "default", cutoff)
```

**注意**：`reclassify_claims` 需要 `LLMExtractor`，在 `_dispatch` 中复用 `self._make_extractor()` 即可。但要注意 `_make_extractor()` 在 production 模式下如果缺 key 会 raise，这是正确行为。

### 修改 3：衰减策略配置化

**问题**：`workers/decay.py` 第 7-14 行的 POLICY dict 和 ACCESS_BONUS 常量硬编码。

**要求**：

将以下参数改为从环境变量读取（保留默认值作为 fallback）：

```python
import os

def _load_policy() -> dict[str, tuple[int, int]]:
    temporal_decay = int(os.getenv("HL_MEM_DECAY_TEMPORAL_DAYS", "90"))
    temporal_archive = int(os.getenv("HL_MEM_DECAY_TEMPORAL_ARCHIVE", "180"))
    permanent_decay = int(os.getenv("HL_MEM_DECAY_PERMANENT_DAYS", "180"))
    permanent_archive = int(os.getenv("HL_MEM_DECAY_PERMANENT_ARCHIVE", "365"))
    return {
        "temporal": (temporal_decay, temporal_archive),
        "permanent": (permanent_decay, permanent_archive),
    }

_ACCESS_BONUS_EVERY = int(os.getenv("HL_MEM_ACCESS_BONUS_EVERY", "10"))
_ACCESS_BONUS_DAYS = int(os.getenv("HL_MEM_ACCESS_BONUS_DAYS", "30"))
_ACCESS_BONUS_CAP = int(os.getenv("HL_MEM_ACCESS_BONUS_CAP", "365"))
```

在 `decay_claims` 函数内部调用 `policy = _load_policy()` 替代模块级 `POLICY`。

**注意**：不要改变默认行为，只是让参数可配置。

## 约束

1. **不要运行 pytest**（Windows 管道兼容性问题），测试由外部执行
2. **不要修改任何测试文件**（tests/ 目录下的文件），只修改源码
3. **向后兼容**：所有现有测试必须通过
4. **不要新增依赖**
5. 完成后运行 `git add -A && git commit -m "refactor: lifecycle state machine + wire dead code + config externalize"`
6. 遵循项目现有代码风格（类型标注、from __future__ import annotations 等）
