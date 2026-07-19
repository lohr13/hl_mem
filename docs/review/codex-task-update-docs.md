# 任务：更新 hl_mem 原始设计文档以反映共识方案

请根据 docs/review/consensus.md 中的共识方案，更新以下原始设计文档。
先读取 docs/review/consensus.md 了解最终决策。

## 需要更新的文件

### 1. docs/implementation-plan.md
- 将 Phase 0 标记为 "Skipped" 并说明原因（用户已有 Hindsight 经验，ADR-0001 方向已定）
- 重命名当前 Phase 1-6，使其反映共识的首版范围
- 首版（Phase 1-2）明确只包含 event + claim + observation 三种类型
- Phase 3-4（原 Observation/Mental Model 和 Experience）标记为"后续迭代"
- 在测试体系中加入"30-50条中文 NER + 检索测试集"要求
- 首版质量门槛中补充：Provider timeout/circuit breaker 生效

### 2. docs/architecture.md
- Section 3.1 记忆类型表：标注首版只实现 event/claim/observation，其余为"后续迭代"
- Section 3.2 volatility：标注首版只有 ephemeral + stable
- Section 4 作用域：标注首版只有 private + shared，但 scope 字段从 Day 1 保留
- Section 5 数据模型：标注首版不建 episodes/traces/policies/procedures 表
- Section 6.2 后台提取：补充 batch 提取、event filter、日 token 预算策略
- Section 10 遗忘：补充 forget 级联删除细节（claim+evidence+embedding BLOB+tombstone）
- Section 11 召回：补充 Provider timeout 2s + circuit breaker
- 新增 Section 或子节：Embedding 策略（text-embedding-v4 2048维，dense+sparse，多 column 设计）
- 新增 Section 或子节：SQLite 写并发策略（单写 Worker 串行化，events 批量 insert）

### 3. docs/HANDOFF.md
- 更新"当前结论"：加入共识方案概述
- 更新"已完成"：加入 review/consensus 过程
- 更新"下一步"：按共识排期重写（Week 1-5）
- 更新"开始编码前需要确认但不阻塞设计的问题"中的 Embedding 选择
- 更新已知风险：加入 text-embedding-v4 批量上限 10条/批的注意事项

### 4. docs/CHANGELOG.md
- 新增 2026-07-20 条目（如果同日则追加）：
  - Decision: 首版 Embedding 改为 text-embedding-v4（Qwen3-Embedding）2048维
  - Decision: 砍掉 Phase 0 基线对比
  - Decision: 首版范围精简（3类型、2档volatility、2档visibility、不建Experience表）
  - Added: Hermes × Codex 三轮 review 共识
  - 影响：所有设计文档

### 5. docs/adr/ 新增 ADR-0002
创建 docs/adr/0002-mvp-scope-and-embedding.md：
- 状态：Accepted
- 背景：ADR-0001 的全量设计工程量大，需要明确首版边界
- 决策：首版范围精简 + Embedding 选型 text-embedding-v4
- 详见 consensus.md

## 约束
- 不要删除任何现有内容，只做增量标注或新增
- 对"首版不实现"的内容，用 `[首版不实现]` 前缀标注，不要删除
- 保持文档现有风格和格式
- 不要动 docs/review/ 目录下的文件
