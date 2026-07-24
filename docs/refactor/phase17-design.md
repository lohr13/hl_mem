# Phase 17: 数据质量全面治理

## 设计原则

四个问题不是独立的，它们共享同一个根因：**canonical_attribute 一个字段扛了太多职责**。
方案 E（slot + tags 分离）是骨架，其余三个问题（去重/TTL/importance）挂在骨架上。

---

## 1. canonical_attribute 职责分离（方案 E）

### 当前问题
- 54 个 canonical_attribute，63% 落入 .other
- 一个字段同时做：检索标签 + 冲突键 + TTL 策略 + 去重隔离
- LLM prompt 只展示了 12/54 个分类

### 目标架构

把 `canonical_attribute` 拆成两个字段：

```
canonical_slot TEXT NULL     -- 可空，受控小集合，参与冲突/去重/TTL
topic_tags TEXT NULL          -- JSON array，多值开放标签，只用于检索
```

**canonical_slot 保留的精确槽（~15个）**：
- preference: ui_theme, response_style, tool_choice
- choice: tool, database, model, provider, memory_system
- config: port, path, env, network
- state: service_health
- identity: name
- plan: deadline
- NULL（开放事实，不参与互斥冲突）

**topic_tags 示例**：architecture, decision, requirement, bugfix, implementation, behavior, dependency, version, migration...

### 关键变化
- 开放事实 `canonical_slot = NULL`，不再伪装成 `fact.other`
- conflict_key 只在 `canonical_slot IS NOT NULL` 时计算
- 无 slot 的事实靠 subject + predicate + value + FTS/vector 召回
- topic_tags 可多值，不进 conflict_key，不隔离去重

### 迁移策略
- 新增 migration：加 `canonical_slot` 和 `topic_tags` 列
- 回填规则：现有 `fact.other` → slot=NULL, tags=["fact"]; 现有非 .other → slot=原值
- prompt 重写：展示完整 slot 列表 + 允许 abstain（返回 null）
- 高置信规则只保留 port/path/env/network/model 等格式可判定的

---

## 2. 跨 subject 语义去重

### 当前问题
- conflict_key = hash(subject + predicate + canonical_attribute)
- subject 不同就不去重，导致 "CN 域名直连" 出现 3 次

### 解决方案

两阶段去重：

**阶段 1：精确去重（现有逻辑，保留）**
- 只对有 canonical_slot 的 claim 做精确 conflict_key 匹配
- 查询条件：相同 slot + 相同 subject_entity_id + 相同 predicate

**阶段 2：跨 subject 语义去重（新增）**
- 对无 slot 的开放事实，按 embedding 相似度 > 0.92 检索候选
- 候选过滤：相同 predicate（不同 subject 允许）
- LLM 判断是否语义等价（复用现有 consolidation judge）
- 安全护栏：高阈值（0.92）+ LLM 二次确认 + dry-run 审计

### 配置
```python
CROSS_SUBJECT_DEDUP_THRESHOLD = 0.92  # embedding 相似度阈值
CROSS_SUBJECT_DEDUP_ENABLED = True    # 开关
```

---

## 3. TTL policy 统一

### 当前问题
- TTL 矩阵：stable + temporal → null（永不过期）
- 导致 7/21 的规划讨论还 active

### 解决方案

TTL 由 scope + volatility + importance 三因子决定：

```python
TTL_MATRIX = {
    # (scope, volatility_band) → TTL days
    ("temporal", "low_importance"):   3,    # importance < 0.4
    ("temporal", "normal"):           7,    # importance 0.4-0.7
    ("temporal", "high_importance"):  30,   # importance > 0.7
    ("permanent", "any"):             None, # 永不过期（需人工 expire）
}
```

**importance band 计算**：
- importance < 0.4 → low_importance
- 0.4 ≤ importance < 0.7 → normal
- importance ≥ 0.7 → high_importance

**volatility 不再独立决定 TTL**——它只影响 importance 的初始值（ephemeral 事实 importance 降 0.2）。

### 回填
- 所有 active 的 temporal claims 重新计算 expires_at
- importance < 0.4 且 age > 3 天的立即 expire

---

## 4. 低 importance 治理

### 当前问题
- importance 0.3 的一次性操作记录和 importance 0.9 的核心偏好有相同生命周期

### 解决方案

**写入门槛**：
- importance < 0.3 的 claim 不写入（直接丢弃）
- 提取 prompt 中明确：一次性操作记录（"空目录已删除"、"从83行精简至35行"）importance 给 0.2

**TTL 联动**：
- 已在问题 3 中解决——importance < 0.4 的 temporal 3 天过期

**importance 计算优化**：
- 提取 prompt 中给出 importance 打分指南：
  - 0.9-1.0：核心身份/永久偏好/关键约束
  - 0.7-0.8：重要架构决策/工具选择/配置
  - 0.5-0.6：项目状态/计划/一般事实
  - 0.3-0.4：一次性操作记录/临时状态
  - < 0.3：不写入

---

## 实施顺序

所有改动作为一个整体设计，分 2 个 Codex 批次执行：

### Batch 1: Schema + 迁移 + 回填 + prompt 重写
- 新增 migration（canonical_slot + topic_tags 列）
- 回填现有数据
- 重写提取 prompt（完整 slot 列表 + importance 指南 + abstain）
- 重写确定性推断规则
- 更新 claim draft 构建逻辑
- 版本 bump → 0.8.0

### Batch 2: 下游适配 + 去重 + TTL
- conflict_key 只用 canonical_slot
- 去重逻辑：精确 slot 去重 + 跨 subject 语义去重
- TTL 矩阵：scope + importance band 三因子
- 低 importance 写入门槛
- 更新召回管线（filter 适配新字段）
- 全部测试适配

---

## 验收指标

- fact.other / .other 概念消除（改为 slot=NULL）
- canonical_slot 非空率 > 40%（说明 LLM 能识别精确槽）
- 跨 subject 重复 < 5 条（当前 ~15 条）
- temporal claims expires_at IS NULL 比例 < 5%
- importance < 0.4 的 temporal claims 3 天内 95% expired
- 249+ tests passed
