# HL-Mem 实施计划

> 交付状态（2026-07-22）：Phase 3–6 已实现；Phase 7 测试体系与质量验证已纳入持续测试。Procedure 不使用独立表，保存在 `policies.procedure` JSON 字段中。SQLite WAL 仍为默认后端，PostgreSQL 仅作为可选适配边界。

- 更新时间：2026-07-20
- 原则：每个 Phase 都必须可单独运行、测试和与基线比较

## Phase 0（Skipped）：建立可复现基线

> [首版不实现] 用户已有 Hindsight 使用经验，且 ADR-0001 已确定总体方向，因此首版跳过基线对比，不阻塞实现；以下内容保留为后续需要横向评测时的参考。

目标：在自研前知道成熟方案在真实负载上的效果。

任务：

- 准备一组中文跨会话场景，包含偏好更新、实时状态过期、矛盾、删除和相似任务复用。
- 在 Hermes 中分别运行 Hindsight Local Embedded 和 MemOS Local Plugin；一次只启用一个。
- 固定主模型、Embedding、问题集和 Judge。
- 记录正确率、Recall@K、stale-hit、注入 token、写入成本和 P95。

验收：

- 同一批数据可以一条命令重复运行。
- 原始输入、召回结果、最终答案和 Judge 结果全部落盘。
- 形成 `docs/benchmarks/baseline-YYYY-MM-DD.md`。

## Phase 1：首版基础设施、事件日志和中文评测集（Week 1）

目标：完成不依赖 LLM 归纳的持久化闭环。

建议目录：

```text
src/hl_mem/
  api/
  domain/
  storage/
  ingest/
  recall/
  workers/
  adapters/hermes/
tests/
  unit/
  integration/
  scenarios/
```

任务：

- Python 项目、配置、结构化日志、类型检查和测试框架。
- SQLite WAL、迁移器和 Repository 接口。
- `events`、`claims`、`evidence_links`、`jobs` 表。
- `POST /v1/events`，支持 Idempotency-Key。
- Fake Extractor、Fake Embedder，测试不依赖外部模型。
- FTS 关键词召回和简单时间过滤。
- `POST /v1/recall` 与证据化 Context Packet。
- Hermes Provider 的 `initialize/prefetch/sync_turn/shutdown`。
- 建立 30–50 条真实中文对话组成的 NER + 检索测试集，同时覆盖实体提取和召回。

首版记忆类型边界：只实现 `event`、`claim`、`observation`，不在本 Phase 引入 Mental Model 或 Experience 通道。

验收：

- 进程重启后记忆仍存在。
- 相同事件重试不会产生重复数据。
- 第三次会话能召回前两次会话信息。
- 召回结果始终包含 Event Evidence。
- Provider 故障不会导致 Hermes 主流程崩溃。

## Phase 2：首版提取、混合检索、遗忘与 Hermes 联调（Week 2–5）

目标：建立可信的事实状态机。

任务：

- JSON Schema Extractor 接口和首个 LLM Provider。
- 实体归一化、时间表达解析和 volatility 分类。
- 双时间 Claim、Conflict Key 和 Claim 状态机。
- 确定性冲突规则和可插拔 LLM Conflict Classifier。
- TTL、refresh signal、历史 `as_of` 查询。
- 显式 `memory_save` 和 `memory_forget`。
- 删除 Tombstone 和 Evidence 级联失效。
- LLM batch 提取、event filter 和日 token 预算。
- text-embedding-v4（Qwen3-Embedding）2048 维 Dense/Sparse Embedding、FTS/BM25、时间过滤与 RRF。
- Observation consolidation、单写 Worker、`events` 批量 insert，以及 Hermes Provider 的 2 秒 timeout、circuit breaker 和无感降级。

首版只处理 `event`、`claim`、`observation` 三种类型；volatility 只启用 `ephemeral` 和 `stable`，visibility 只启用 `private` 和 `shared`。

验收场景：

1. “我喜欢深色模式”随后“现在改用浅色模式”：当前查询只给新值，历史查询能给出演化。
2. 两个同权威来源给出不同值：返回 disputed，而不是随机选一个。
3. 五分钟前的服务状态已过 TTL：当前查询要求重新验证，历史查询仍可返回观察。
4. 用户删除一个偏好：原文、Claim、Embedding 和相关派生结果不能再次召回。

## Phase 3（后续迭代）：Mental Model 和扩展维护

> [首版不实现] Observation 的最小规则已前移到 Phase 2；本 Phase 保留完整 Observation consolidation 与 Mental Model、Session Summary 和高级维护设计，待首版稳定后实现。

目标：让系统形成可撤销、可解释的长期归纳。

任务：

- Observation consolidation，支持 proof count 和时间叙事。
- Mental Model 定义、source watermark 和增量刷新。
- stale dependency 检测与重建。
- Session Summary 作为导航索引。
- decay、expire、archive Worker。
- Job lease、重试、dead-letter 和管理 CLI。

验收：

- 多个独立证据可生成 Observation。
- 删除或撤回证据后，相关模型自动 stale 并重建。
- 重复执行同一维护任务结果一致。
- Worker 中途退出后可以续跑，不重复生成派生记录。
- 没有 Evidence Link 的 Observation/Mental Model 无法进入 active。

## Phase 4（后续迭代）：经验、Reward、Policy 和 Procedure

> [首版不实现] Experience 通道首版不建表、不写 Repository；以下语义和验收要求保留，未来通过新增 migration 和 Repository 实现。

目标：复用 Agent 的成功经验，而不只是记住对话事实。

任务：

- Episode 拼接和 Trace 提取。
- 用户反馈、任务成功信号和 Reward 归因。
- 跨独立 Episode 的 Policy 候选池。
- Policy gain、support 和 candidate/active/retired 状态。
- Procedure 结晶、probationary/active/retired 生命周期。
- Procedure 召回与使用结果回写。

验收：

- 单次偶然成功不能直接形成 Active Procedure。
- 多次成功经验可以生成候选 Procedure。
- Procedure 连续失败后会降级或退休。
- Procedure 的每个步骤能追踪到支持 Episode。

## Phase 5（后续迭代）：扩展召回、MCP 和管理能力

> 首版所需的 FTS/BM25、Dense、时间过滤和 RRF 已前移到 Phase 2。
>
> [首版不实现] MCP Server、跨 Agent 共享、扩展召回通道和管理 UI 延后实现；以下内容保留为目标设计。

目标：达到可日常使用和跨 Agent 共享的状态。

任务：

- Dense Embedding 接口及本地/远程 Provider。
- BM25/FTS、Dense、Fact、Temporal、Relation、Procedure 多通道。
- RRF、MMR、可选 Reranker。
- Query Router 和 token budget packer。
- MCP Server。
- CLI：inspect、explain、export、import、forget、maintenance。
- 最小管理 UI 或只读调试页面。

验收：

- Hermes 与至少一个其他 MCP Agent 能共享受控作用域的记忆。
- 不同用户、项目、Agent 的 private 数据互不可见。
- 所有召回可 explain。
- P95 和 token 预算达到项目设定阈值。

## Phase 6（后续迭代）：生产化和规模迁移

> [首版不实现] 仅在 SQLite 或单机 Worker 经测量成为瓶颈后启动。

触发条件：SQLite 或单机 Worker 已经通过测量成为瓶颈。

候选任务：

- PostgreSQL + pgvector 存储适配器。
- 多 Worker 租约、队列与水平扩展。
- 加密、备份、恢复和 Schema Migration 演练。
- 多租户配额、审计和保留策略。
- 只有多跳图召回收益明确时才引入专用图后端。

## 测试体系

### 单元测试

- 时间区间、双时间版本和 TTL。
- Conflict Key、确定性矛盾规则和状态转移。
- Evidence DAG、级联 stale 和删除。
- Decay、Archive 和 Procedure 生命周期。
- Namespace/Visibility 权限矩阵。
- Job 幂等、租约和失败恢复。

### 集成测试

- SQLite 真数据库迁移和事务。
- API → Job → Worker → Recall 全链路。
- Hermes Provider 与 Fake daemon。
- 进程重启、并发重试和重复 Hook。

### 场景测试

- 30–50 条真实中文对话的 NER + 检索测试集，覆盖实体提取和召回。
- 用户偏好发生变化。
- 项目从 PostgreSQL 迁移到 MySQL。
- 实时价格、天气、服务状态到期。
- 两个来源产生不可解冲突。
- Agent 自己曾经回答错误，后续不能将错误自我强化。
- 用户执行 forget 后，异步任务不能复活内容。
- 多次任务成功产生 Procedure，后续失败导致退休。
- 多用户群聊中的 speaker/audience 隔离。

### 公共 Benchmark

- [LongMemEval](https://github.com/xiaowu0162/longmemeval)：信息提取、多会话推理、知识更新、时间推理、拒答。
- [LoCoMo](https://github.com/snap-research/locomo)：超长对话问答和事件总结。
- [EvoMemBench](https://github.com/DSAIL-Memory/EvoMemBench)：跨 Episode 的知识和执行经验复用。

公共 Benchmark 不能替代项目场景测试，尤其不能覆盖 TTL、级联删除、权限和 Prompt Injection。

## 首版质量门槛

在进入日常使用前至少满足：

- 原始 Event 丢失率为 0。
- 无证据的 Active 派生记忆数量为 0。
- 显式删除测试通过率 100%。
- Namespace 越权召回数量为 0。
- 当前状态召回中过期 Claim 数量为 0。
- 所有迁移均可在备份副本上重复执行。
- Hermes Provider 关闭或 daemon 故障时，Hermes 能降级运行。
- Hermes Provider 的 2 秒 timeout 和 circuit breaker 生效。

准确率、Recall@K、P95 和 token 阈值仍需根据真实负载评测后填写；Phase 0 已跳过，不以其作为首版开工前置条件。
