# HL-Mem 已完成里程碑与路线图

> 基线：v0.15.0 + 2026-07-28 未发布治理修订。发布历史和逐版本测试基线以 [CHANGELOG](CHANGELOG.md) 为准，能力成熟度以
> [Capability Matrix](capability-matrix.md) 为准。

HL-Mem 已完成最初 Phase 0–6 的建设目标。本文不再作为待实现任务单，而是记录已交付里程碑与后续演进方向。

## Milestone 0（✅ Completed）：可复现评测基线

- 建立 extraction、retrieval、lifecycle 三层离线评测与 LongMemEval adapter。
- 固化输入、召回结果、指标报告和可重复 CLI 流程。
- 保留 LoCoMo、EvoMemBench 与真实中文跨会话场景作为后续横向评测方向。

## Milestone 1（✅ Completed）：事件溯源与本地持久化

- 完成 Python 3.11+ 工程、SQLite WAL、FTS5、迁移器和 Repository 分层。
- 交付幂等 Event 摄入、Claim、Evidence、Job、Fake Provider 与证据化 Context Packet。
- 完成 FastAPI、Hermes Provider 以及进程重启和故障降级路径。

## Milestone 2（✅ Completed）：可信事实与混合召回

- 交付双时间 Claim、实体归一化、slot + tags、冲突状态机和三层去重。
- 接入 LLM 提取、Embedding、FTS + dense + RRF 与可选 Reranker（具体模型和维度由 `.env` 配置）。
- 完成 TTL、历史 `as_of`、显式保存/遗忘、上下文预算和原子写入事务。

## Milestone 3（✅ Completed）：派生记忆与生命周期

- 完成 Observation 与 Mental Model 的证据依赖、stale 传播和维护 Worker。
- 交付 expire、decay、archive、重分类、Job lease/retry/progress 与维护 CLI。
- 保证派生记忆可解释、可撤销，并在源 Claim 撤回后失效。

## Milestone 4（✅ Completed）：Experience、Policy 与 Procedure

- 完成 Episode、Trace、reward/outcome 反馈和跨 Episode Policy 归纳。
- 实现 Policy 生命周期；Procedure 保存在 `policies.procedure` JSON 字段，不使用独立表。
- 将 Tool/Procedure intent 接入召回和使用结果反馈。

## Milestone 5（✅ Completed）：接口、扩展召回与运维

- 完成 REST、Hermes、五工具 MCP、CLI、在线备份、审计日志和 LLM spans。
- 交付 query expansion、关系候选、tag soft boost、多因子排序、reranker 与 context packing。
- 所有核心能力通过配置注入，外部 Provider 失败可降级到本地确定性路径。

## Milestone 6（✅ Completed）：工程收敛与生产基线

- 完成分层依赖门禁、状态机统一、配置集中化、错误分类和统一 retry/timeout。
- 交付不可变 SQL migrations、SQLite 在线备份、CI 质量门禁和 production launcher。
- PostgreSQL 当前仅有实验性连接探针；SQLite 仍是唯一具备完整 HL-Mem 存储语义的后端。

## Roadmap

路线图遵循“以评测和真实瓶颈驱动演进”，不代表已承诺版本：

1. 评估 relation proposal 和 usefulness 数据，决定是否将 `audit` / `observe` 提升到自动模式。
2. 接入真实图片输入源，评估视觉描述器的准确率、成本和隐私边界。
3. 持续调优中文提取、Mental Model 推理、召回质量与 token/P95 预算。
4. 补强多租户的隔离、配额、审计和 retention 策略。
5. 仅在 SQLite 或单机 Worker 被测量为瓶颈后，实现 PostgreSQL 存储语义与水平扩展。
6. 仅在多跳图召回相对现有 relation expansion 有明确收益时，引入专用图后端。

具体候选设计见 [Feature Proposals](proposals/)，已完成能力和默认开关见
[Capability Matrix](capability-matrix.md)。
