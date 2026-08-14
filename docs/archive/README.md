# HL-Mem 历史文档归档

本目录保存已经实施、被替代或仅供研究复盘的历史设计。归档内容保持当时的源码基线、配置、模型、路径和
评测口径，**不是当前 API、默认值、部署步骤或路线图的规范来源**。

当前行为以 [`architecture.md`](../architecture.md)、[`configuration.md`](../configuration.md)、
[`api.md`](../api.md)、[`capability-matrix.md`](../capability-matrix.md) 和
[`CHANGELOG.md`](../CHANGELOG.md) 为准。归档文档与当前实现冲突时，以这些活文档和代码为准。

## Design

- [`audit-log-design.md`](design/audit-log-design.md)：审计日志的早期设计；主体能力已经实现。
- [`memory-management-design.md`](design/memory-management-design.md)：生命周期与 TTL 的早期设计；当前契约见架构和配置文档。

## Proposals

- [`00-overview.md`](proposals/00-overview.md)：v0.11.2 六项能力总览。
- [`01-query-expansion.md`](proposals/01-query-expansion.md)：受控查询扩展。
- [`02-relation-discovery.md`](proposals/02-relation-discovery.md)：audit-first 关系发现。
- [`03-benchmark-suite.md`](proposals/03-benchmark-suite.md)：早期 benchmark 分层方案。
- [`04-image-evidence.md`](proposals/04-image-evidence.md)：图片证据入口。
- [`05-feedback-lifecycle.md`](proposals/05-feedback-lifecycle.md)：反馈与 usefulness 生命周期。
- [`06-procedure-intent.md`](proposals/06-procedure-intent.md)：Tool / Procedure intent。
- [`07-quality-trends.md`](proposals/07-quality-trends.md)：质量趋势基础设施。
- [`extraction-prompt-optimization.md`](proposals/extraction-prompt-optimization.md)：提取 prompt 分层与准入设计。
- [`recall-improvement-proposal.md`](proposals/recall-improvement-proposal.md)：v0.16.1 召回改进方案。

以上 proposal 均不代表仍在排期；完成状态和最终行为应从 CHANGELOG、能力矩阵和代码判断。

## Releases

- [`v0.20.0-tokenized-fts.md`](releases/v0.20.0-tokenized-fts.md)
- [`v0.20.1-watchdog-observability.md`](releases/v0.20.1-watchdog-observability.md)
- [`v0.20.2-recall-quality-supervision.md`](releases/v0.20.2-recall-quality-supervision.md)
- [`v0.21.2-conflict-resolution.md`](releases/v0.21.2-conflict-resolution.md)

完整发布历史以 CHANGELOG 和 GitHub Releases 为准。

## Research

- [`competitor-comparison.md`](research/competitor-comparison.md)：2026-07 的竞品能力快照。
- [`memos-vs-hindsight.md`](research/memos-vs-hindsight.md)：早期技术选型比较。
- [`plan-lifecycle-research.md`](research/plan-lifecycle-research.md)：未实施的计划类记忆生命周期研究，不属于当前路线图。

归档研究中的外部产品能力、价格和接口可能已经变化，不应用作当前事实。
