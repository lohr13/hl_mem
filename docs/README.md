# HL-Mem 设计文档入口

本文档目录是 `hl_mem` 项目的长期交接入口。设计、实现状态和架构决策应落在仓库里，不依赖任何一次对话的上下文。

## 冲突归并运行配置

M5 的 `consolidate_conflicts` 任务默认每天本地时间 03:30 幂等入队。以下参数均可通过环境变量覆盖：

```text
HL_MEM_CONSOLIDATE_CRON=03:30
HL_MEM_CONSOLIDATE_BATCH_SIZE=100
HL_MEM_CONSOLIDATE_CONFIDENCE=0.8
```

归并判定复用 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL` 与 `LLM_TIMEOUT`。Job payload 可设置
`dry_run=true`，用于输出分类统计且不修改 claim 状态。

## 新对话或新开发者的阅读顺序

1. [HANDOFF.md](HANDOFF.md)：当前状态、已完成内容、下一步和未决问题。
2. [research/memos-vs-hindsight.md](research/memos-vs-hindsight.md)：MemOS、Hindsight 与需求的适配结论。
3. [adr/0001-core-strategy.md](adr/0001-core-strategy.md)：已选择的总体技术路线及被放弃的方案。
4. [architecture.md](architecture.md)：目标架构、数据模型、写入、召回、矛盾和遗忘算法。
5. [implementation-plan.md](implementation-plan.md)：分阶段实现计划、验收条件和测试策略。
6. [CHANGELOG.md](CHANGELOG.md)：文档、决策与实现变化的时间线。

## 文档维护规则

- 每次实现或设计修改后，必须更新 [HANDOFF.md](HANDOFF.md) 的更新时间、已完成事项和下一步。
- 每次产生可交接的设计或实现变化时，在 [CHANGELOG.md](CHANGELOG.md) 的最新日期下增加一条记录。
- 改变已经确定的架构决策时，不要直接重写历史 ADR；新增一个 ADR，并把旧 ADR 标为 `Superseded`。
- 数据表、API 或状态机变化时，同步修改 [architecture.md](architecture.md)。
- 里程碑、任务顺序或验收条件变化时，同步修改 [implementation-plan.md](implementation-plan.md)。
- 外部项目的行为结论应附官方仓库或官方文档链接，并记录核对日期。
- 对尚未实现或未经测试的内容使用“计划”“候选”或“假设”，避免写成已完成事实。

## 项目目标摘要

HL-Mem 是一个面向 Hermes 及其他 Agent 的本地优先、跨会话记忆服务。它需要同时支持：

- 跨对话、跨 Agent、按用户和项目隔离的持久记忆；
- 原始事件、事实知识、经验、程序性技能和派生总结的分层管理；
- 实时状态与长期知识的不同生命周期；
- 有来源、有时间、有版本的矛盾检测和事实演化；
- 基于重要性、使用效果和时间的降权、归档与删除；
- 可解释召回：能够说明为什么保存、为什么召回、依据了哪些证据；
- 通过 Hermes `MemoryProvider`、MCP 和 REST 接入，不修改宿主 Agent 核心。
