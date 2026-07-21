# HL-Mem 项目交接状态

> 最后更新：2026-07-21

## 当前状态

- **分支**：`main`
- **阶段**：首版完成，93 测试全绿
- **服务**：FastAPI on port 8200，LLM=GLM-5.1，Embedding=text-embedding-v4 (2048d)
- **存储**：SQLite WAL + FTS5 + 向量 BLOB（`var/hl_mem.db`）

## 已完成

- 首版 3 种记忆类型（event + claim + observation）
- LLM 提取（百炼 Coding Plan AK）+ Embedding（百炼通用 AK）
- 三层去重：fact_hash → conflict_key → semantic (cosine > 0.95)
- 多因子排序 + LLM reranker
- 衰减策略：temporal 90/180d, permanent 180/365d, access_count bonus
- Hermes Provider 适配器（commit 248093f）
- 架构重构：删除空 domain/，合并 FakeEmbedder，拆分 pipeline，归档历史文档

## 下一步

- 调试偏好变更 supersede 边界 case
- 接入 Hermes MemoryProvider 正式替换试跑
- 根据实际使用反馈调优提取 prompt 和召回质量
- Phase 3（Mental Model）/ Phase 4（Experience 通道）按需开启

## 关键文档索引

| 文档 | 说明 |
|------|------|
| [architecture.md](architecture.md) | 当前已实现架构 |
| [implementation-plan.md](implementation-plan.md) | 实现计划 |
| [adr/0001-core-strategy.md](adr/0001-core-strategy.md) | 核心策略决策 |
| [adr/0002-mvp-scope-and-embedding.md](adr/0002-mvp-scope-and-embedding.md) | 首版范围 + Embedding 选型 |
| [review/consensus.md](review/consensus.md) | 首版共识 |
| [review/optimization-consensus.md](review/optimization-consensus.md) | 优化共识 |
| [archive/](archive/) | 历史任务单和中间讨论 |

## 已知风险

- LLM 提取会产生假事实 → 必须保留原始证据
- 中文实体归一化/时间表达比英文更容易出错 → 独立中文测试集
- 自动遗忘可能误删低频关键信息 → 首版只降权和归档，不物理删除
- `text-embedding-v4` 批量上限 10 条/批 → 需异步受控并发
