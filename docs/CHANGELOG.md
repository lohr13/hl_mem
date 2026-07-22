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
- Implemented：首版完整实现（Week 1-5），包括事件日志、LLM提取、混合检索、矛盾检测、TTL、遗忘、Worker、Hermes Provider。
- Implemented：Prompt 调优（中文值保持、predicate 标准化、conflict 检测修复）。
- Verified：qwen3.7-plus + text-embedding-v4 端到端验证通过。
- Impact：全部 `src/` 和 `tests/`。
## 2026-07-22 — Phase 3–7

- 增加带 proof count、source watermark、证据准入和 stale 传播的派生记忆维护。
- 完成 Episode、Trace、反馈归因以及内嵌 Procedure 的 Policy 生命周期。
- 增加确定性查询路由、RRF/MMR、预算装箱、MCP 工具契约和 CLI 导入导出。
- 增加可选 PostgreSQL 连接边界、SQLite 在线备份恢复、租户配额和保留策略。
- SQLite WAL 仍是默认后端，离线测试不依赖外部 API 或 PostgreSQL。
