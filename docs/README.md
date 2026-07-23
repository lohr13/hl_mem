# HL-Mem 设计文档入口

本文档目录是 `hl_mem` 项目的长期交接入口。设计、实现状态和架构决策应落在仓库里，不依赖任何一次对话的上下文。

## 阅读顺序

1. [HANDOFF.md](HANDOFF.md)：当前状态、已完成内容、下一步和未决问题。
2. [CHANGELOG.md](CHANGELOG.md)：版本变更时间线（v0.3.0 包含架构重构 + 冲突检测 + 数据质量三个大阶段）。
3. [architecture.md](architecture.md)：目标架构、数据模型、写入、召回、矛盾和遗忘算法。
4. [adr/0001-core-strategy.md](adr/0001-core-strategy.md)：总体技术路线及被放弃的方案。
5. [adr/0002-mvp-scope-and-embedding.md](adr/0002-mvp-scope-and-embedding.md)：首版范围 + Embedding 选型。
6. [implementation-plan.md](implementation-plan.md)：分阶段实现计划、验收条件。
7. [research/memos-vs-hindsight.md](research/memos-vs-hindsight.md)：MemOS、Hindsight 与需求的适配分析。

## 架构重构记录

v0.3.0 经历了完整的架构重构（Phase 0-12），各阶段详细记录：

| 阶段 | 文档 | 核心内容 |
|------|------|----------|
| Phase 1 (P0) | [refactor-phase1-p0.md](refactor-phase1-p0.md) | 数据正确性：事务原子化、fact_hash v2 |
| Phase 2 (Domain) | [refactor-phase2-domain.md](refactor-phase2-domain.md) | 统一 ClaimStatus + EpisodeStatus + 状态守卫 |
| Phase 3 (Services) | [refactor-phase3-services.md](refactor-phase3-services.md) | 共享 Application Services |
| Phase 4 (Deps) | [refactor-phase4-dependencies.md](refactor-phase4-dependencies.md) | 依赖方向修正 |
| Phase 5 (Dedup) | [refactor-phase5-dedup.md](refactor-phase5-dedup.md) | 合并 Hermes providers + 工厂集中化 |
| Phase 6 (Maintenance) | [refactor-phase6-maintenance.md](refactor-phase6-maintenance.md) | schemas 拆分 + 配置集中化 |
| Phase 7 (Quality) | [refactor-phase7-quality.md](refactor-phase7-quality.md) | Protocol + errors + retry + router 合并 |
| Phase 8 (Hardfix) | [refactor-phase8-hardfix.md](refactor-phase8-hardfix.md) | observation recall + conflict + context budget |
| Phase 9 (Improvements) | [refactor-phase9-improvements.md](refactor-phase9-improvements.md) | relations + multimodal + extractor routing |
| Phase 10-11 (Conflict) | [refactor-phase10-conflict-fix.md](refactor-phase10-conflict-fix.md) · [phase11](refactor-phase11-mutual-exclusivity.md) | 白名单互斥模型 + 冲突检测修复 |
| Phase 12 (Quality) | [refactor-phase12-quality.md](refactor-phase12-quality.md) | 实体归一化 + dedup 0.82 + attribute reconcile + TTL |

完整重构提案见各阶段 `*-proposal.md` 文件。

## 配置参考

关键环境变量（完整列表见根目录 `.env.example`）：

```text
# 运行模式
HL_MEM_ENV=dev                    # dev | production
HL_MEM_DB_PATH=var/hl_mem.db

# LLM 提取
LLM_API_KEY=***                   # 百炼 Coding Plan AK
LLM_BASE_URL=https://coding.dashscope.aliyuncs.com/v1
LLM_MODEL=glm-5.2                 # 或 qwen3.7-plus

# Embedding
EMBEDDING_API_KEY=***             # 百炼通用 AK
EMBEDDING_DIM=2048
EMBEDDING_MODEL=text-embedding-v4

# Reranker
HL_MEM_RERANKER=on                # off | fake | on | real

# 去重 / 冲突
HL_MEM_DEDUP_THRESHOLD=0.82       # 语义去重阈值

# Worker
HL_MEM_WORKER_POLL_INTERVAL=2.0
HL_MEM_WORKER_MAINTENANCE_INTERVAL=600
```

## 文档维护规则

- 每次实现或设计修改后，更新 [HANDOFF.md](HANDOFF.md) 的更新时间、已完成事项和下一步。
- 每次产生可交接的变化时，在 [CHANGELOG.md](CHANGELOG.md) 的最新版本下增加一条记录。
- 改变已确定的架构决策时，不要直接重写历史 ADR；新增一个 ADR，并把旧 ADR 标为 `Superseded`。
- 数据表、API 或状态机变化时，同步修改 [architecture.md](architecture.md)。
- 对尚未实现或未经测试的内容使用"计划""候选"或"假设"，避免写成已完成事实。
