# Phase 11：冲突检测互斥性模型重构

## 背景

Phase 10 修复了 `.other` 通配符假冲突，但修复后实测发现仍有假冲突产生。根因是当前互斥性模型的默认值反了——大多数槽位实际上不是互斥的。

## 实测数据（修复后新增）

修复后处理了 69 条新 claim，仍产生 12 条假冲突：

### 假冲突分布

| conflict_key 组 | 槽位 | 条数 | 根因 |
|---|---|---|---|
| a3d532bde | plan.deadline | 6 | 同一实体多个并行计划，不互斥 |
| 171245ae9 | choice.tool + fact.tool_choice | 2 | CONFLICT_SLOT_ALIASES 跨谓词合并 |
| df49709a8 | choice.tool + fact.tool_choice | 2 | 同上 |
| 2ca815508 | choice.tool | 1 | 用户同时用多个工具 |
| 9d9f8b57 | config.env | 1 | 用户有多个环境变量 |

### 核心问题

**当前逻辑**（Phase 10）：默认所有槽位互斥，只有 `.other`/`memory.explicit`/`custom.unknown` 豁免。

**现实**：大多数槽位不互斥：

| 槽位 | 实际语义 | 互斥？ |
|---|---|---|
| config.env | HTTP_PROXY, LLM_TIMEOUT, NO_PROXY... | ❌ |
| choice.tool | Codex, Hermes, V2RayN... | ❌ |
| plan.deadline | 多个并行计划 | ❌ |
| config.path | 多个路径 | ❌ |
| config.port | 一个服务一个端口 | ✅ |
| config.model | 一个端点一个模型 | ✅ |
| identity.name | 一个人一个名字 | ✅ |
| identity.account | 一个账号 | ✅ |
| identity.role | 一个角色 | ✅ |
| state.service_health | 同一时间一个状态（需配合 state_change） | ✅ |

## 三个硬伤

### 硬伤 1：CONFLICT_SLOT_ALIASES 跨谓词合并太激进

`CONFLICT_SLOT_ALIASES` 把 `choice.tool`、`fact.tool_choice`、`preference.tool_choice` 全合并成 `tool_choice`。导致跨谓词的不相关事实被强制比较。

### 硬伤 2：大多数具体槽位非互斥

Phase 10 只豁免了 `.other`，但 `config.env`、`choice.tool`、`plan.deadline`、`config.path` 等全部不互斥。

### 硬伤 3：低价值噪声漏网

15 条低价值 claim（value < 5 字符如 "串行"、"180"、"ok"）未被过滤。

## Hermes 的建议方案（供 Codex 评估）

### 方案：翻转互斥性默认值

```python
# 建议逻辑: 默认非互斥，只有 MUTUALLY_EXCLUSIVE_SLOTS 中的才判冲突
MUTUALLY_EXCLUSIVE_SLOTS = frozenset({
    "config.port",
    "config.model",
    "identity.name",
    "identity.account",
    "identity.role",
    "state.service_health",
})
```

同时：
- `CONFLICT_SLOT_ALIASES` 移除或大幅缩减（至少移除跨谓词别名）
- 低价值过滤：在 `_claim()` 中加 `len(value.strip()) < 5` 的后置校验

### 需要 Codex 评估的问题

1. `MUTUALLY_EXCLUSIVE_SLOTS` 列表是否完整？有没有遗漏应该互斥的？
2. `state.service_health` 互斥但需要配合 `state_change` 逻辑——当前 predicate 归一化是否覆盖？
3. `CONFLICT_SLOT_ALIASES` 全删 vs 保留部分？如果保留，哪些是真正有用的？
4. 低价值过滤放在 LLM 提取器 `_claim()` 中 vs 放在 ingest 管线中，哪个更合适？
5. 向后兼容：180 个测试中哪些可能受影响？
