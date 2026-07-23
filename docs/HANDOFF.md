# HL-Mem 项目交接状态

> 最后更新：2026-07-23 · v0.3.0

## 当前状态

- **分支**：`main`
- **版本**：v0.3.0
- **阶段**：架构重构完成（Phase 0-12），220 测试全绿
- **服务**：FastAPI on port 8200，LLM=glm-5.2，Embedding=text-embedding-v4 (2048d)，Reranker=gte-rerank-v2
- **存储**：SQLite WAL + FTS5 + 向量 BLOB（`var/hl_mem.db`），14 migrations

## 已完成

### 核心功能

- 3 种记忆类型（event + claim + observation）
- LLM 提取（前序上下文 + 时间锚定 + ADD-only）+ Embedding（2048d）+ Reranker
- 三层去重：fact_hash v2 → conflict_key（白名单互斥）→ semantic (best-match, 0.82)
- 冲突检测：确定性 ConflictResolver（5 slots）+ LLM ConflictConsolidator（灰区）
- 数据质量：实体归一化 + canonical attribute reconcile + scope 后置规则 + TTL policy
- 混合召回：FTS5 BM25 + Dense Vector → RRF → 多因子排序 → Reranker → 上下文预算打包
- Experience 通道：Episode + Trace + Policy + 奖励回传
- 生命周期：TTL 过期 → 线性衰减 → 归档 → 重分类
- 显式遗忘：级联撤回 + 向量清除 + stale 传播
- Hermes Provider（358 行，2s timeout + circuit breaker）
- MCP Server（4 工具契约）
- 审计日志 + 在线备份 + CLI 导入导出
- 可选 PostgreSQL 后端

### 架构重构（v0.3.0）

- P0 数据正确性：事务原子化 + fact_hash v2 + MCP pipeline 修复
- 分层架构：api/ → application/ → domain/core/ → storage/
- 状态机统一：ClaimStatus + EpisodeStatus 集中到 lifecycle.py
- 配置集中化：config.py + settings.py + components.py
- Provider 合并：删除冗余 adapter
- P2 质量：Protocol 接口化、错误分类化、retry 工具化

## 下一步

- 接入 Hermes MemoryProvider 正式替换试跑
- 根据实际使用反馈调优提取 prompt 和召回质量
- Mental Model 推理增强（基础已实现）
- 多租户（架构设计保留）

## 关键文档索引

| 文档 | 说明 |
|------|------|
| [CHANGELOG.md](CHANGELOG.md) | 版本变更时间线 |
| [architecture.md](architecture.md) | 当前已实现架构 |
| [implementation-plan.md](implementation-plan.md) | 实现计划 |
| [adr/0001-core-strategy.md](adr/0001-core-strategy.md) | 核心策略决策 |
| [adr/0002-mvp-scope-and-embedding.md](adr/0002-mvp-scope-and-embedding.md) | 首版范围 + Embedding 选型 |
| [refactor-phase*.md](.) | 架构重构各阶段详细记录 |
| [review/consensus.md](review/consensus.md) | 首版共识 |
| [review/optimization-consensus.md](review/optimization-consensus.md) | 优化共识 |
| [archive/](archive/) | 历史任务单和中间讨论 |

## 已知风险

- LLM 提取可能产生假事实 → 原始证据链保留
- 中文实体归一化/时间表达容易出错 → 独立中文测试集
- 自动遗忘可能误删低频关键信息 → 首版只降权和归档，不物理删除
- `text-embedding-v4` 批量上限 10 条/批 → 异步受控并发
