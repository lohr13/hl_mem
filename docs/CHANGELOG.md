# HL-Mem 变更记录

本文件记录影响跨对话交接的设计与实现变化。代码提交历史仍由 Git 保存；这里记录“为什么改”和“哪些文档或模块受影响”。

格式：

```text
## YYYY-MM-DD

- Added/Changed/Fixed/Removed：变化及原因。
- 影响：相关文档、模块、迁移或 ADR。
```

## 2026-07-20

- Added：建立文档入口、交接状态、MemOS/Hindsight 选型分析、核心 ADR、系统架构和分阶段实施计划。
- Added：完成 Hermes × Codex 三轮 review 并形成一致接受的首版共识，新增 ADR-0002 固化范围和 Embedding 选型。
- Decision：HL-Mem 使用统一的事件溯源双通道设计，不让 Hermes 同时加载 MemOS 与 Hindsight。
- Decision：事实通道参考 Hindsight，经验与程序性通道参考 MemOS Local Plugin。
- Decision：MVP 使用 SQLite 和关系边表，不预先引入 Neo4j，也不把 Hindsight/MemOS 设为运行时硬依赖。
- Decision：首版 Embedding 改为阿里 `text-embedding-v4`（Qwen3-Embedding）2048 维 Dense+Sparse，记录模型版本并保留多 column 接口。
- Decision：砍掉 Phase 0 基线对比；用户已有 Hindsight 经验，ADR-0001 已确定方向。
- Decision：首版范围精简为 3 种记忆类型（event/claim/observation）、2 档 volatility（ephemeral/stable）、2 档 visibility（private/shared），[首版不实现] Experience 表。
- 影响：`docs/README.md`、`docs/HANDOFF.md`、`docs/research/memos-vs-hindsight.md`、`docs/adr/0001-core-strategy.md`、`docs/architecture.md`、`docs/implementation-plan.md`。
- 影响：所有设计文档；本轮具体更新 `docs/HANDOFF.md`、`docs/architecture.md`、`docs/implementation-plan.md`、`docs/CHANGELOG.md` 和 `docs/adr/0002-mvp-scope-and-embedding.md`。
