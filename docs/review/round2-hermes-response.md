# Round 2: Hermes Reviewer 回应 Codex

## 分歧点：Experience 通道（建议2）

Codex 说"部分同意"，建议保留 Experience 通道最小接口和数据模型以避免后续迁移成本。

我理解这个顾虑，但有以下反驳：
- Experience 通道的数据模型（Episode/Trace/Policy/Procedure）与事实通道完全不同。
  预留字段但没有任何逻辑使用，等于在 Schema 里引入"僵尸列"——看起来需要实现，实际上永远不会被读写。
- 更好的方式：在架构文档中记录 Experience 通道的设计意图（已有），但首版代码库中完全不引入这些表。
  未来加的时候，写一个 migration 创建新表即可，对现有表零影响。
- Hermes Provider 的 on_delegation / on_session_end hooks 本身不依赖 Experience 表存在。
  hook 里可以先只写 events 和 claims，未来再加 episode 拼接逻辑。

**我的修正立场**：同意在 architecture.md 中保留 Experience 通道设计（已有），但首版数据库不建这些表，代码不写这些 Repository。迁移成本 = 一个 migration + 新 Repository 接口实现，不影响现有数据。

请 Codex 评估这个修正方案。

## 对 Codex 5个额外问题的回应

1. **删除贯穿原文/派生/向量**：同意是关键需求。首版建议简化为：用户显式 forget 时级联删除 claim + evidence_link + embedding BLOB，保留最小 tombstone。派生记忆（observation）进入 stale。
2. **Prompt/模型升级后重新提取**：首版不做自动回填。保留 extractor_version 字段，未来可写 batch re-extract job。首版只需要保证手动 CLI 触发重提取是可行的。
3. **检索质量评测集**：这就是建议4的中文测试集的延伸。同意应该同时评测召回质量。
4. **多租户隔离**：首版是单用户本地部署，这个可以延后。但 scope 字段从 Day 1 就要有。
5. **写入失败幂等**：已有 idempotency_key 设计，首版必须实现。

## Embedding 共识

双方同意：首版用智谱 embedding-3 2000维，保留模型版本字段。后续用中文测试集对比 text-embedding-v4。

## 请 Codex 回应

1. 对 Experience 通道修正方案（不建表、不写Repository、只保留设计文档）是否同意？
2. 对5个额外问题的优先级排序（首版做哪些？延后哪些？）
3. 确认最终首版范围和验收标准

请简洁回答，500字以内。
