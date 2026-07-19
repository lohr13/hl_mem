# ADR-0002：收敛首版范围并采用 text-embedding-v4

- 状态：Accepted
- 日期：2026-07-20
- 决策者：项目发起人、Hermes Agent 与 Codex

## 背景

ADR-0001 定义了统一事件溯源双通道记忆服务的完整目标设计，但全量实现同时包含事实、Observation、Mental Model、Experience、MCP、多租户和生产化能力，首版工程量过大。项目需要在不破坏长期演进路径的前提下，明确可在 5 周内交付和验证的边界，并确定面向中文检索的默认 Embedding 方案。

用户已有 Hindsight 使用经验，ADR-0001 的总体方向也已确定，因此继续执行 Phase 0 基线对比不会改变首版开工决策。

## 决策

首版范围收敛如下：

- 记忆类型只实现 `event`、`claim`、`observation`。
- volatility 只启用 `ephemeral` 和 `stable`。
- visibility 只启用 `private` 和 `shared`，完整 scope 字段从 Day 1 保留。
- 存储采用 SQLite WAL + FTS5 + Dense/Sparse 向量 BLOB；写入经单写 Worker 串行化，`events` 批量 insert。
- 写入必须支持 `idempotency_key`；提取采用 LLM batch、event filter 和日 token 预算。
- 检索采用 FTS/BM25、Dense Embedding、时间过滤和 RRF。
- forget 级联清理 Claim、Evidence Link、Embedding 和索引，并留下最小 Tombstone；受影响 Observation 标记为 `stale`。
- Hermes Provider 设置 2 秒 timeout、circuit breaker，并在 daemon 故障时无感降级。
- 跳过 Phase 0 基线对比，直接按 Week 1–5 排期实现和评测。

[首版不实现] Mental Model、Experience 通道（Episode/Trace/Policy/Procedure）、MCP Server、自动 re-extract 回填和多租户隔离。Experience 首版不建表、不写 Repository，仅在架构文档中保留目标设计，未来通过新增 migration 和 Repository 接入。

首版默认 Embedding 采用阿里 `text-embedding-v4`（Qwen3-Embedding）2048 维，并使用 Dense+Sparse 输出。每条向量记录模型和版本，存储接口保留多 embedding column 设计；智谱 `embedding-3` 作为 fallback。Sparse 向量首版按稳定的 `index→weight` 格式序列化为 BLOB，记录格式版本和端序。

`text-embedding-v4` 每批最多 10 条，调用端采用异步受控并发、10 条满批、离线 Batch API、增量缓存、QPS 动态限流和重试退避。

## 后果

正面后果：

- 5 周首版边界明确，可优先验证幂等写入、中文提取与检索、时间有效性、遗忘和 Hermes 降级。
- Experience 延后但迁移路径明确，不影响现有数据模型。
- Dense+Sparse 为中文语义检索和精确词项检索提供互补信号。

负面后果：

- 首版不能复用跨任务经验，也不提供 MCP 和完整多租户隔离。
- 10 条批量上限增加请求和限流管理复杂度。
- Embedding 质量和阈值仍需用 30–50 条真实中文对话测试集实测确认。

## 参考

完整共识、逐条表态、排期和验收标准见 [review/consensus.md](../review/consensus.md)。
