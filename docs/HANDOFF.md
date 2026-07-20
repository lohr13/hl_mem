# HL-Mem 项目交接状态

- 最后更新：2026-07-20
- 当前阶段：首版（Phase 1-2）开发完成，35 测试全绿，真实 API 验证通过
- 当前分支：`main`
- 当前负责人：待定

## 当前结论

HL-Mem 不直接复刻 MemOS 或 Hindsight，也不让 Hermes 同时加载两个外部记忆 Provider。

当前选定路线是：

- 以 Hindsight 的“原始事实 → Observations → Mental Models”和时间化冲突处理为事实记忆参考；
- 以 MemOS Local Plugin 的“Episode/Trace → Reward → Policy → Skill → World Model”为经验和程序性记忆参考；
- 自己实现统一的事件日志、双时间事实、生命周期维护、权限作用域与召回打包；
- 对 Hermes 暴露为一个独立的 `hl_mem` MemoryProvider；
- [首版不实现] 对其他 Agent 暴露 MCP 接口；首版保留 REST 接口，MCP 后续迭代。

详细原因见 [adr/0001-core-strategy.md](adr/0001-core-strategy.md)。

2026-07-20 三轮 review 后的首版共识进一步收敛为 5 周交付：只实现 `event`、`claim`、`observation`，volatility 只启用 `ephemeral`/`stable`，visibility 只启用 `private`/`shared`（完整 scope 字段从 Day 1 保留）；Experience/Mental Model/MCP/多租户均为 [首版不实现]。存储采用 SQLite WAL + FTS5 + 向量 BLOB，Embedding 默认使用 `text-embedding-v4`（Qwen3-Embedding）2048 维 Dense+Sparse，并实现幂等写入、批量提取、混合召回、forget 级联以及 Hermes Provider 的 timeout/circuit breaker/降级。

该共识由 [ADR-0002](adr/0002-mvp-scope-and-embedding.md) 固化，详细讨论见 [review/consensus.md](review/consensus.md)。

## 已完成

- [x] 调研 Mem0、Zep/Graphiti、Hindsight、MemOS、PowerMem、LangMem、Letta、Cognee 和 A-MEM。
- [x] 深入核对 MemOS Local Plugin 的 Hermes Adapter、L1/L2/L3、Reward、Skill 和三层召回设计。
- [x] 深入核对 Hindsight 的事实、Observations、Mental Models、后台 consolidation 和时间冲突处理。
- [x] 确定 HL-Mem 的总体分层和核心数据模型。
- [x] 定义热路径、后台维护、矛盾检测、遗忘和召回流程。
- [x] 制定分阶段实现计划与验收指标。
- [x] 完成 Hermes × Codex 三轮 review，形成并一致接受 `review/consensus.md` 首版方案。
- [x] 将首版范围、Embedding 选型和 5 周排期固化为 ADR-0002。
- [x] Week 1: 项目骨架 + SQLite Schema + API + FTS (7 tests)
- [x] Week 2: LLM Extractor + Event Filter + Token Budget (19 tests)
- [x] Week 3: Embedding + 去重 + Conflict + Observation + Forget (28 tests)
- [x] Week 4: Worker + TTL + Hermes Provider (33 tests)
- [x] Week 5: 真实 API 端到端验证 + prompt 调优 (35 tests)
- [x] Prompt 调优：中文值保持 + predicate 标准化 + conflict 修复

## 下一步

- 调试偏好变更 supersede 边界 case（LLM 对“改用”的提取一致性）
- 接入 Hermes MemoryProvider 正式替换 Hindsight 试跑
- 根据实际使用反馈调优提取 prompt 和召回质量
- 考虑开启 Phase 3（Mental Model）或 Phase 4（Experience 通道）

## 开始编码前需要确认但不阻塞设计的问题

以下项目应在实现 Phase 1 时选择默认值，并记录新 ADR：

其中 Python 包管理、向量存储和 Embedding 默认值已经由共识/ADR-0002 确认；原问题保留用于追踪决策来源。

- Python 包管理使用 `uv` 还是 Poetry。建议 `uv`。
- MVP 向量索引使用 SQLite 向量扩展，还是先使用 BLOB + 小规模暴力余弦。建议先抽象接口，测试中用 Fake Embedder。
- 向量存储默认值已确认：首版使用 Dense/Sparse BLOB + 小规模暴力余弦，接口仍保持可替换。
- 默认 LLM/Embedding Provider。设计要求可插拔，不能绑定单一云服务。
- Embedding 默认值已确认：阿里 `text-embedding-v4`（Qwen3-Embedding）2048 维 Dense+Sparse；记录模型版本并保留多 embedding column 接口，智谱 `embedding-3` 作为 fallback。仍需通过中文测试集确认质量阈值，但不阻塞编码。
- 原始会话事件默认保留期。建议单用户本地部署默认永久保留，但允许用户配置和显式删除。
- 首版是否直接依赖 Hindsight。当前建议不作为运行时硬依赖，只作为基线和算法参考。

## 已知风险

- LLM 提取结果会产生假事实，必须保留原始证据，且 Agent 自己的回答默认不能成为高可信事实。
- 中文实体归一化、代词消解和时间表达比英文更容易出错，应单独建立测试集。
- 自动遗忘容易删除低频但关键的信息；首版只做降权和归档，不自动物理删除。
- 同一个用户在不同 Agent、项目和群聊中的记忆不能默认互通，必须经过作用域和可见性过滤。
- 图数据库会显著增加复杂度；在关系表和混合召回未证明不足前，不引入 Neo4j。
- Vendor benchmark 分数不可直接比较；必须在相同模型、相同数据和相同 Judge 设置下复现。
- `text-embedding-v4` 批量上限为 10 条/批，相比 `embedding-3` 的 64 条会显著增加请求数；需采用异步受控并发、10 条满批、离线 Batch API、增量缓存、QPS 动态限流和重试退避。

## 新会话交接提示模板

可将下面内容直接发给新的 Agent：

```text
请先完整阅读 hl_mem/docs/README.md 和其中规定的交接文档顺序。
以 hl_mem/docs/HANDOFF.md 的“下一步”为当前任务来源。
不要推翻既有 ADR；若需要改变决策，请新增 ADR 并更新 HANDOFF。
完成代码或设计修改后，必须同步更新 HANDOFF 和相关设计文档。
```
