# Phase 2：统一领域状态

## 背景

Codex 审查发现 P1-4（状态机不完整）、P1-12（Episode 状态不一致），以及 update_status() 仍接受任意字符串。

## 项目位置

`D:/workspace/hl_agent/hl_mem/`

---

## 修改 1：补全 ClaimStatus 枚举

### 问题

`candidate` 状态在 `pipeline.py:152` 中使用（`claim["status"] = "candidate"`），但不在 `ClaimStatus` 枚举里。如果通过 MCP 或其他入口调用 `assert_transition()`，candidate 状态会报错。

### 修复

在 `src/hl_mem/lifecycle.py` 中：

1. `ClaimStatus` 添加 `CANDIDATE = "candidate"`
2. `ALLOWED_TRANSITIONS` 添加：
   - `(CANDIDATE, ACTIVE)` — 审核后激活
   - `(CANDIDATE, DISPUTED)` — 审核后发现冲突
   - `(CANDIDATE, EXPIRED)` — 候选过期
   - `(CANDIDATE, ARCHIVED)` — 候选归档
   - `(CANDIDATE, RETRACTED)` — 候选撤回

---

## 修改 2：新增 EpisodeStatus 枚举

### 问题

`experience/service.py` 中 `TERMINAL_EPISODE_STATUSES = {"success", "failed", "cancelled"}`，但：
- `induce_policies.py:21` 仍查询 `status IN ('success','completed')`，`completed` 不再存在于 schema 中
- migration 010 定义的是 `running/success/failed/cancelled`
- Episode 状态转换散落在 ExperienceService 各方法中，没有统一守卫

### 修复

在 `src/hl_mem/lifecycle.py` 中新增：

```python
class EpisodeStatus(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"

TERMINAL_EPISODE_STATUSES = frozenset({
    EpisodeStatus.SUCCESS,
    EpisodeStatus.FAILED,
    EpisodeStatus.CANCELLED,
})

ALLOWED_EPISODE_TRANSITIONS: frozenset[tuple[EpisodeStatus, EpisodeStatus]] = frozenset({
    (EpisodeStatus.RUNNING, EpisodeStatus.SUCCESS),
    (EpisodeStatus.RUNNING, EpisodeStatus.FAILED),
    (EpisodeStatus.RUNNING, EpisodeStatus.CANCELLED),
})

def assert_episode_transition(from_status: str, to_status: str) -> None:
    """断言 Episode 状态转换合法。"""
    try:
        transition = (EpisodeStatus(from_status), EpisodeStatus(to_status))
    except ValueError as error:
        raise InvalidTransitionError(
            f"invalid episode status transition: {from_status} -> {to_status}"
        ) from error
    if transition not in ALLOWED_EPISODE_TRANSITIONS:
        raise InvalidTransitionError(
            f"invalid episode status transition: {from_status} -> {to_status}"
        )
```

然后：

1. `experience/service.py` 中的 `TERMINAL_EPISODE_STATUSES` 和 `InvalidStateTransitionError` 改为从 `lifecycle.py` 导入（或保留 ExperienceService 内部使用 lifecycle 的定义）
2. ExperienceService 中每次状态变更前调用 `assert_episode_transition()`
3. **删除 `induce_policies.py` 中 `'completed'` 引用**，只保留 `'success'`

---

## 修改 3：收紧 update_status()

### 问题

`storage/repository.py` 的 `update_status()` 接受任意字符串，绕过 lifecycle 守卫。

### 修复

在 `update_status()` 中集成 `assert_transition()`：

```python
def update_status(self, claim_id: str, status: str, commit: bool = True) -> bool:
    from hl_mem.lifecycle import assert_transition, ClaimStatus
    # 验证目标状态是合法的 ClaimStatus
    try:
        ClaimStatus(status)
    except ValueError:
        raise ValueError(f"invalid claim status: {status}")
    cursor = self.connection.execute("UPDATE claims SET status=? WHERE id=?", (status, claim_id))
    if commit:
        self.connection.commit()
    return cursor.rowcount == 1
```

注意：这里只验证目标状态是合法枚举值，不强制检查转换路径（因为转换合法性由调用方在变更前通过 `assert_transition()` 确保）。不强制全量转换检查是因为某些批量操作（如 ttl.py 的 `WHERE status='active'` 批量过期）不可能逐条调用 assert_transition。

---

## 修改 4：DB CHECK 约束

### 问题

数据库层面没有 CHECK 约束阻止非法 status 值。

### 修复

新建 `src/hl_mem/storage/migrations/012_status_check.sql`：

```sql
-- Add CHECK constraints on status columns.
-- SQLite doesn't support adding CHECK to existing columns directly,
-- so we use a pragma to verify and document the constraint.

-- For new databases, the constraint is enforced via application code.
-- This migration records the intent and adds a lightweight validation
-- table for startup pre-checks.

INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES ('012_status_check', datetime('now'));
```

注意：SQLite 的 ALTER TABLE 不支持加 CHECK。真正的 CHECK 约束需要重建表（CREATE TABLE _new + INSERT + DROP + RENAME），对于生产数据有风险。**这里只记录 migration 标记，实际约束由应用层 lifecycle 守卫保证**。如果你认为需要表重建，可以不做，只用应用层守卫。

---

## 修改 5：统一 ExperienceService 状态引用

### 问题

`experience/service.py` 自己定义了 `TERMINAL_EPISODE_STATUSES` 和 `InvalidStateTransitionError`，应统一到 lifecycle.py。

### 修复

1. `experience/service.py` 从 `lifecycle.py` 导入 `EpisodeStatus`、`TERMINAL_EPISODE_STATUSES`、`assert_episode_transition`、`InvalidTransitionError`
2. 删除 service.py 中的 `TERMINAL_EPISODE_STATUSES` 和 `InvalidStateTransitionError` 定义
3. 保留 `InvalidStateTransitionError` 作为 `InvalidTransitionError` 的别名以兼容现有 import：
   ```python
   # experience/service.py
   from hl_mem.lifecycle import (
       EpisodeStatus,
       TERMINAL_EPISODE_STATUSES,
       assert_episode_transition,
       InvalidTransitionError as InvalidStateTransitionError,
   )
   ```
   这样现有代码 `except InvalidStateTransitionError` 不需要改。

---

## 约束

1. **不要运行 pytest**
2. **不要修改 tests/ 目录下的任何文件**
3. **向后兼容**：现有 180 个测试必须全部通过
4. **不要新增依赖**
5. **不要问任何问题**
6. 完成后 `git add -A && git commit -m "refactor(domain): unified ClaimStatus + EpisodeStatus + status guard enforcement"`
