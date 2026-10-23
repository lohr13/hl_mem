# HL-Mem 1.1.0 最终设计

## 1. 定位、基线与发布分支

- 开发基线：`1.0.0rc1` 后的 `3f50072`。
- 产品目标：在不破坏 1.x 稳定契约的前提下，让实体召回更可靠，并用真实 Provider、真实插件和持续观测证明 1.0 建立的扩展底座确实可用。
- 开发周期：四周开发与验证，随后进行 48 小时 RC 真实运行观察。
- 成本原则：实体召回不得新增 LLM 调用；真实模型验证使用一次性数据库和硬预算，不建设大型付费验证集。

发布线采用明确的双线模型：

- `main` 在 `v1.0.0` 标签发布前只接受 1.0 的缺陷、安全和发布元数据修改。
- `develop/1.1` 承载全部 1.1 功能；功能任务继续使用独立 worktree 和短分支，完成后合并到 `develop/1.1`。
- 1.0 的 P0/P1 修复先进入 `main`，当天同步到 `develop/1.1`。
- `v1.0.0` 发布后，将最终的 `main` 合并到 `develop/1.1`；在此之前禁止把 1.1 功能合回 `main`。

1.1.0 是功能次版本，不替代 1.0.1。1.0.1 只用于稳定版缺陷和安全修复。

## 2. 最终范围

### 2.1 做

- 将已有的查询实体约束从只观察升级为高置信实体的默认强约束。
- 把实体范围下推到 FTS 和 Dense 候选读取之前，避免候选窗口先被其他实体占满。
- 使用真实 LLM、Embedding 和 Reranker 完成一次性数据库中的摄入、提取、召回、重排和用量结算验证。
- 建设核心仓库之外的 `hl-mem-provider-dashscope` 参考插件，以真实服务验证三类稳定 Provider 契约。
- 按真实职责拆分 `RecallService` 和 `LLMExtractor`，并同步降低其复杂度预算。
- 删除 PostgreSQL 连通性探针，清理已退役 extraction pre-filter 和独立 Tag channel 的当前公开描述。
- 新增 `hl-mem ops report`，提供费用、调用、延迟、错误、任务和 SQLite 运行状态报告。
- 使用现有 Core 1.0 公开回归、24 条实体针对性回归、真实 Provider 冒烟和 48 小时 RC 观察作为发布证据。

### 2.2 不做

- 不增加实体识别、实体消歧或实体召回专用 LLM 调用。
- 不引入新的独立召回通道、永久排序因子或在线学习权重。
- 不建设 200 题大型验证集，不宣称未经测量的 Recall 百分比提升。
- 不引入 Neo4j、Graphiti、Graph 数据库双写、ANN/HNSW 或新存储后端。
- 不扩展 Provider Plugin API 到 REST 路由、CLI、任务、Migration、存储或安全策略。
- 不建设插件市场，不引入 pluggy，不自动安装或启用第三方代码。
- 不全仓重排目录，不拆 Settings、Hermes 和 Repository 等与本轮目标无直接关系的热点。
- 不移除 0.x 配置迁移墓碑、历史 fixture、Migration 快照或归档研究材料。
- 不改变 Query Expansion、关系发现、LLM dedup 等昂贵能力的默认关闭状态。

## 3. 精确实体召回

### 3.1 现状与问题

1.0 已有 `recall.entity_constraint_mode`、确定性 alias 解析、typed canonical entity、Claim 实体链接和 `observe` Trace。当前约束发生在每个 FTS/Dense 通道已经按 `candidate_limit` 截断之后，因此正确实体的 Claim 若未进入窗口，后续过滤无法补回。1.1 不重建实体系统，只修复约束位置和查询表达。

### 3.2 高置信条件

只有同时满足以下条件才允许强约束：

1. 查询命中一个或多个不重叠的 active alias mention。
2. 所有 mention 唯一解析到同一个 typed canonical entity。
3. alias proof 均有效，未出现跨类型同名或多个 active alias 目标。
4. 该实体的 active Claim 链接覆盖检查完整。

任一条件不满足即保持宽召回。解析、链接查询或 SQLite 读取失败也保持宽召回，并在 Trace 记录明确原因；不得返回空结果冒充成功。

### 3.3 查询计划

`QueryEntityResolution` 增加规范化 mention span，`QueryEntityPlan` 生成：

- `entity_id`：唯一高置信 canonical entity。
- `residual_query`：从原始查询中删除实体 mention 后保留的属性、动作和时间词。
- `search_query`：`residual_query` 非空时使用它；为空时保留原始查询。
- `scope_mode`：`entity`、`observe`、`wide` 或 `off`。

高置信查询使用一次 `search_query` Embedding 替代原查询 Embedding，不同时计算两份向量，因此每次召回的 Embedding 调用次数不增加。低置信和歧义查询继续使用原查询。

### 3.4 候选下推

实体范围是现有 FTS/Dense 通道的读取约束，不是第三个融合通道：

- FTS 在 `MATCH`、namespace、双时间和状态条件之外，通过 `claim_entity_links`/canonical subject/target 约束实体，再执行 `LIMIT`。
- Dense 在读取向量行之前使用相同实体范围，只对该实体的有界可见 Claim 计算相似度；该路径不依赖外部 Graph，也不增加模型调用。
- 当 `residual_query` 为空时，仍只在实体范围内使用原查询完成现有两通道排序，不创建无界 entity-only 浏览模式。
- `sqlite_vec` 的普通查询保持不变；高置信实体查询使用实体范围内的本地精确扫描。实体链接索引先缩小集合，避免为了一个内部过滤接口扩大公开向量后端契约。
- 候选继续经过现有 RRF、可见性、去重、Reranker、相关性和交付逻辑；不新增实体专属分数。

默认模式从 `observe` 改为 `enforce`。这是 Beta 默认行为调整，更新配置文档、能力矩阵和 Changelog；显式配置 `off` 或 `observe` 的部署保持原值。

### 3.5 Trace 与故障边界

Trace 记录但不存储原始查询全文：

- mention 数量和 proof ID。
- 解析置信级别、canonical entity ID 和实体类型。
- residual term 数量。
- FTS/Dense 约束前后候选数量、实体路径耗时和最终 Top-K 贡献。
- 未启用强约束的确定原因。

实体逻辑失败时不得触发 Query Expansion、额外 LLM 或静默空结果；唯一降级路径是原有宽召回。

## 4. 真实 Provider 验证

### 4.1 验证形态

在 `benchmarks/provider/` 增加显式运行、默认不进入普通 CI 的 live smoke。它创建一次性配置、一次性数据库和独立 usage sidecar，使用正式 LLM、Embedding 和 Reranker完成：

1. 中英文 Event 摄入与批量提取。
2. Claim、Evidence、实体链接和向量持久化。
3. 普通召回、精确实体召回、时间查询和偏好查询。
4. Reranker 成功与受控失败回退。
5. Provider reserve、attempt、settle/release 和聚合报告闭环。
6. Worker 关闭、SQLite 关闭和临时资源清理。

验证工件只记录模型标识、配置 fingerprint、输入 fixture hash、调用量、Token、项目数、文档数、估算费用、延迟、错误分类和最终状态；不得保存密钥、请求正文、响应正文或真实生产 Claim。

### 4.2 硬预算

单次完整运行最多：

- 10 次 LLM 请求。
- 30 个 Embedding 项目。
- 100 篇 Reranker 文档。
- 20 元估算费用。

预算在调用前由现有 `UsageGovernor` 原子预留；预算未知且配置了金额上限时继续 fail-closed。整个 1.1 发布周期的正式 live evidence 总预算上限为 50 元，超出必须获得新的人工授权。

真实 Provider 失败不写入生产数据库，也不降低为 Fake Provider。可恢复的 429、超时和上游错误沿用宿主有限重试；最终失败形成明确报告并阻断对应发布门禁。

## 5. 真实 Provider 插件

### 5.1 独立发行物

在核心仓库之外建设 `hl-mem-provider-dashscope`：

- 独立 `pyproject.toml`、版本和 wheel。
- 通过固定 Entry Point group `hl_mem.providers` 注册。
- 提供 DashScope LLM、Embedding 和 Reranker 三类稳定能力。
- 只导入 `hl_mem.plugins` 的公共类型，不导入核心内部 `components`、`llm`、`storage` 或 `workers`。
- 只构造中立 `ProviderRequest`、解析 `ProviderResponse`；HTTP、重试、预算、审计、指标和错误归一化继续由宿主负责。

内置 DashScope Provider 在 1.x 内继续保留，避免破坏兼容。参考插件的价值是验证第三方包、版本协商、配置命名空间和宿主代理，而不是立即搬空内置实现。

### 5.2 实证门禁

参考插件必须：

- 在干净虚拟环境中从 wheel 安装。
- 未加入 allowlist 时不导入插件代码。
- 加入 allowlist 后通过 `doctor`、Registry 和 `/healthz` 检查。
- 使用真实 DashScope 服务跑完第 4 节的同一 live smoke。
- 证明三类调用全部进入宿主 usage ledger，且无活动 reservation 遗留。
- 证明缺失、重复、版本不兼容和错误配置均 fail-closed。
- 证明插件调用失败不影响未使用该插件的内置 Provider。

Plugin API 只接受向后兼容的加法修复。稳定成员若暴露设计缺陷，使用新增可选字段或新 minor contract 解决，并保留旧成员；不在 1.1 删除或改写既有稳定签名。

参考插件形成独立可安装开源仓库和版本化 wheel。任何 PyPI 发布仍遵守单独的发布授权，不由测试或 CI 自动上传。

## 6. 热点拆分

拆分以职责和依赖方向为标准，不以文件行数本身作为完成条件。

### 6.1 Recall

- `application/recall.py`：保留 `RecallRequest`、`RecallService` 公共门面、用例编排和兼容 patch point。
- `recall/query_planning.py`：承载 Query Expansion session、Embedding 查询选择和实体查询计划组合。
- `recall/entity_query.py`：承载确定性解析、残余查询、候选范围和 Trace 数据。
- `application/recall_side_effects.py`：承载曝光、访问计数、重试和失败审计。
- 现有 `application/recall_delivery.py`：继续承载上下文包、交付和反馈附件，不复制组装逻辑。

依赖方向保持 `application -> recall/domain/storage protocol`。`recall` 模块不得反向导入应用服务。

### 6.2 Extraction

- `ingest/llm_extractor.py`：保留 `LLMExtractor` Facade、构造参数、公开属性和一次提取调用的顶层编排。
- `ingest/extraction/orchestrator.py`：承载 chunk、auto split、delta repair、请求重试和结果合并状态机。
- `ingest/extraction/verification.py`：承载 entailment verifier 调度、用量记录和失败审计。
- 现有 `prompts.py`、`schema.py`、`parsing.py`、`repair.py`、`postprocessing.py` 继续作为单一实现源。

拆分不得改变 Prompt、Schema、AdmissionPolicy、Provider 调用次数、Event 幂等性、Claim 事务和测试 patch point 的业务语义。完成一个热点后立即把复杂度预算降低到实际值，不为后续增长预留额外空间。

## 7. 实验遗留清理

删除：

- `src/hl_mem/storage/postgres.py` 及只验证“缺少驱动”的对应测试。
- README、能力矩阵和架构文档中把 PostgreSQL 探针描述为当前能力的内容。
- 当前文档中把 extraction pre-filter、独立 Tag channel 描述为可启用能力的内容。
- 已失效的生产导入、示例配置和脚本参数。

保留：

- `config.loader` 和 `config.migrate` 对 `extraction.pre_filter`、`recall.tag_channel_enabled`、`recall.tag_channel_weight` 的 retired-key 识别。
- v0.36.1 配置迁移 fixture、Migration 快照、Changelog 和 `docs/archive/` 历史材料。
- Tag 分类与稳定 soft boost。
- Image Provider experimental preview 与关系发现 Beta。

因此清理不会让旧配置被静默接受，也不会破坏升级说明。

## 8. 运行观测

### 8.1 命令面

新增稳定、只读命令：

```text
hl-mem ops report --since 24h
hl-mem ops report --since 7d --json
```

`--since` 只接受正整数加 `h` 或 `d`，最大 30 天。无数据返回零值报告；usage sidecar 或主库损坏时明确失败，不自动创建、修复或重写账本。

### 8.2 报告内容

复用现有 usage ledger、Job 表、Worker runtime 和 SQLite 文件状态，输出：

- 按 capability/provider/model/status 聚合的请求数。
- input/output/total Token、Embedding 项目、Reranker 文档、图片数和估算费用。
- 成功率、未知 outcome、未知费用、错误分类、P50/P95 延迟。
- active/expired reservation 和最近一次失败时间。
- pending/running/failed/dead Job 数量及按任务类型分组。
- Worker 最近活动、维护失败和 recall side-effect 积压。
- 主数据库、WAL、SHM 和 usage sidecar 大小。
- conflict manual backlog 和最老待处理时长。

人类输出突出异常和预算使用率；JSON 使用版本化 schema 并进入契约测试。报告不包含查询、Claim、Prompt、响应、endpoint、插件配置或密钥。

`/healthz` 继续保持轻量，只增加 stale reservation、当日失败数和预算使用率摘要；7 天聚合和延迟分位数只由显式 CLI 计算，避免高频探活产生昂贵查询。

### 8.3 报警边界

报告将以下状态标为 warning 或 failure，但不自动执行修复：

- 日预算使用率达到 80%。
- 出现 expired reservation 或 unknown outcome。
- 存在 failed Job，或 running lease 已超过允许时间。
- WAL 大小超过主库大小或 256 MiB 中的较大者。
- Worker 长于两个有效 poll interval 未活动。

1.1 不发送邮件、Webhook 或外部监控事件；通知集成不进入本轮范围。

## 9. 兼容、安全与数据边界

- REST、MCP、配置 schema、导入导出、备份格式和三类稳定 Provider Plugin API 保持 1.x 向后兼容。
- `entity_constraint_mode` 已是 Beta；默认值改变必须写入 Changelog，显式用户值不被迁移改写。
- 本轮不需要主数据库 Migration。实体链接和索引已存在；运行观测只读取既有表和文件。
- 参考插件是受信任的进程内代码，不宣传为沙箱；allowlist 继续阻止“安装即执行”。
- 所有新 Trace 和报告字段先做敏感信息审查，使用 hash、计数、枚举和稳定 ID，禁止原始模型内容。
- 实体约束必须继续执行 namespace、valid time、recorded time、status 和 intent 可见性规则，不得因实体命中绕过历史查询语义。
- `ops report` 使用只读连接，不运行 Migration，不恢复 reservation，不重试 Job。

## 10. 验收门禁

### 10.1 实体召回

- 24 条针对性回归覆盖唯一 active alias、历史 alias、跨类型同名、多实体、无实体、链接不完整、中英文和空 residual；历史 alias 只验证不发生错误强约束。
- 高置信用例的正确 Claim 全部进入 Top 5。
- 跨实体错误 Top 1 为 0。
- 低置信、歧义和链接不完整用例与 1.0 宽召回结果一致。
- Core 1.0 的 Recall@5、MRR、hard/soft abstention precision/recall 下降均不超过 `0.01`。
- HTTP 成功率 100%，forbidden-status hit 和新增 LLM 调用均为 0。
- 实体查询 P95 不超过 `max(基线 + 10ms, 基线 × 1.10)`。

这些是有界回归门禁，不构成对真实总体 Recall 的统计声明。

### 10.2 Provider、插件与观测

- 内置 Provider 和外部插件各完成一次真实 live smoke，且在硬预算内成功。
- usage reservation、attempt 和 settlement 与真实调用数一致，运行结束 active reservation 为 0。
- 真实日志、Trace、报告和测试工件通过密钥与正文泄漏扫描。
- 外部插件 wheel 在干净环境安装、发现、禁用、启用、冲突和失败隔离全部通过。
- `ops report` 的人类输出和 JSON schema 在空数据、正常数据、失败数据和损坏数据下行为确定。

### 10.3 工程质量

- Python 3.12、3.13、3.14 全量测试和覆盖率 80% 门禁继续通过。
- Ruff、Black、isort、mypy、构建、import boundary、OpenAPI、MCP、配置、Plugin API 和 Migration 门禁全部通过。
- Recall 和 Extraction 拆分前后的冻结行为、调用次数、错误类型、审计和事务结果一致。
- 复杂度 ratchet 不扩大；完成拆分的两个热点必须降低自身 ceiling。
- wheel 不包含 live Provider 密钥、结果、临时数据库、参考插件源码或研究缓存。

## 11. 实施顺序

1. 第 1 周：冻结 1.1 基线；实现 `ops report`；建立内置 Provider live smoke 和成本证据。
2. 第 2 周：实现实体 residual query 与候选下推；完成 24 条回归、Core 1.0 对比和 24 小时 observe 运行。
3. 第 3 周：把高置信默认切换为 enforce；建设外部 DashScope 参考插件并跑真实服务；拆分 Recall 热点。
4. 第 4 周：拆分 Extraction 热点；清理实验遗留；完成全量、安全、构建、插件和文档门禁；发布 `1.1.0rc1`。
5. RC 阶段：真实运行 48 小时，只修 P0/P1。生产语义、稳定契约或数据修复需要发布 `rc2` 并重新开始 48 小时；纯文档修正不重置。
6. 观察通过后发布 `1.1.0`；PyPI、GitHub Release 和参考插件发布均要求单独的最终发布授权。

## 12. 完成定义

HL-Mem 1.1.0 完成时必须同时满足：

- 高置信实体查询在候选窗口之前约束 FTS/Dense，歧义查询保持宽召回。
- 实体提升不产生新增 LLM 调用，也不增加正常查询的 Embedding 调用次数。
- 真实 LLM、Embedding、Reranker 和外部 Provider 插件都有受预算约束的成功证据。
- 运行者可以通过一个只读命令看到费用、延迟、失败、任务和 SQLite 健康状态。
- RecallService 与 LLMExtractor 的职责边界清晰，冻结行为和公共兼容面不变。
- 仓库不再宣称支持 PostgreSQL 探针、extraction pre-filter 或独立 Tag channel，但旧配置仍会收到明确迁移错误。
- 1.0 稳定发布线与 1.1 开发线互不污染，所有 1.0 修复已同步进入 1.1。
- 当前文档、能力矩阵、默认值、契约快照和实际行为一致。
