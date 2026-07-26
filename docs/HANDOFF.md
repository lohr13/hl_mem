# HL-Mem 项目交接状态

> 最后更新：2026-07-26 · v0.14.2

## 当前状态

- **分支**：`main`
- **版本**：v0.14.2
- **阶段**：v0.14.2
- **服务**：FastAPI on port 8200；LLM/Embedding/Reranker 均通过 `.env` 配置（见 `.env.example`），当前部署使用 glm-5.2 + text-embedding-v4 (2048d) + gte-rerank-v2
- **存储**：SQLite WAL + FTS5 + 向量 BLOB（`var/hl_mem.db`），29 migrations，约 403 active / 514 total claims
- **FTS**：trigram（claims/tags），unicode61（events）

## 已完成

### 核心功能

- 3 种记忆类型（event + claim + observation）
- LLM 提取（前序上下文 + 时间锚定 + ADD-only）+ Embedding + Reranker（模型和维度均由 `.env` 配置）
- 三层去重：fact_hash v2 → conflict_key（白名单互斥）→ semantic (best-match, 0.82)
- 冲突检测：确定性 ConflictResolver（5 slots）+ LLM ConflictConsolidator（灰区）
- 数据质量：实体归一化 + canonical attribute reconcile + scope 后置规则 + TTL policy
- 混合召回：FTS5 BM25 + Dense Vector → RRF → 多因子排序 → Reranker → 上下文预算打包
- Experience 通道：Episode + Trace + Policy + 奖励回传
- 生命周期：TTL 过期 → 线性衰减 → 归档 → 重分类
- 显式遗忘：级联撤回 + 向量清除 + stale 传播
- Hermes Provider（2s timeout + circuit breaker）
- MCP Server（5 工具契约，可嵌入工具套件，beta）
- REST API 新增 `POST /v1/extract/dry-run`、`POST /v1/consolidate`
- LLM 可观测性：`llm_call_spans` 持久化调用 span，`healthz` 暴露 24h 聚合
- 审计日志 + 在线备份 + CLI 导入导出
- 实验性 PostgreSQL 连接探针（尚无 HL-Mem 存储语义）

### v0.12.0 六大特性

- 多查询召回：默认 `auto`，短/指代查询按需扩展，失败回退原始 query
- 关系候选发现：默认 `audit`，只记录 proposal，不自动写边
- Benchmark suite：LongMemEval adapter + extraction/retrieval/lifecycle 三层指标，CLI 按需运行
- 图片证据入口：视觉描述与证据落库已实现，默认 `off`
- 反馈驱动维护：usefulness 聚合已接入，默认 `observe`，不影响 TTL/decay
- Tool/Procedure intent：Experience pipeline 确定性路由，默认 `keyword`

### 架构重构（v0.10.0）

- P0 数据正确性：事务原子化 + fact_hash v2 + MCP pipeline 修复
- 分层架构：api/ → application/ → domain/core/ → storage/
- 状态机统一：ClaimStatus + EpisodeStatus 集中到 lifecycle.py
- 配置集中化：config.py + settings.py + components.py
- Provider 合并：删除冗余 adapter
- P2 质量：Protocol 接口化、错误分类化、retry 工具化

## 下一步

- 观察 relation proposal 准确率与 usefulness 聚合数据，再评估 `auto` / `on` 模式
- 接入实际图片输入源后评估开启视觉描述器
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
| [archive/refactor/](archive/refactor/) | 架构重构各阶段历史记录 |
| [api.md](api.md) | REST API 端点与请求约定 |
| [capability-matrix.md](capability-matrix.md) | 能力成熟度与默认模式 |
| [archive/reviews/consensus.md](archive/reviews/consensus.md) | 首版共识（历史归档） |
| [archive/reviews/optimization-consensus.md](archive/reviews/optimization-consensus.md) | 优化共识（历史归档） |
| [archive/](archive/) | 历史任务单和中间讨论 |

## 已知风险

- LLM 提取可能产生假事实 → 原始证据链保留
- 中文实体归一化/时间表达容易出错 → 独立中文测试集
- 自动遗忘可能误删低频关键信息 → 首版只降权和归档，不物理删除
- 当前 embedding 模型批量上限 10 条/批（取决于模型，见 `.env` 配置） → 异步受控并发
