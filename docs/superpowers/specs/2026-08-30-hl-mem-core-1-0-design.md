# HL-Mem Core 1.0 最终设计

## 1. 定位与基线

- 冻结基线：`v0.36.1 / 2dbb6a9`。
- 产品定位：精致的单机记忆产品与成熟开源项目。
- 设计目标：核心语义可靠、默认行为可解释、模型效果优先、插件边界稳定、功能强而不臃肿。
- 实施周期：六周开发与验证，加一周 RC 观察。
- 非目标：企业多租户、分布式执行、高可用、合规平台和为了形式整齐进行全仓改名。

Core 1.0 不是对现有系统推倒重来。现有 `application/domain/ingest/recall/storage/workers` 分层、SQLite 权威存储、双时间 Claim、证据链、生命周期和审计体系继续作为主干；本轮只修正真实缺口、收缩危险默认值、建立值得长期维护的公共扩展面，并拆分确有职责混杂的热点。

## 2. 最终能力范围

### 2.1 做

- Event、Evidence、Claim、Entity、Relation 与双时间 Claim 模型。
- 文本摄入、结构化 LLM 提取、Embedding、FTS + Dense + RRF、可选 Reranker。
- Entity 归一、slot、Tag soft boost、确定性关系、人工关系及有界关系扩展。
- 保守去重、确定性 L0 冲突、冲突案卷、人工 delegation 和完整审计。
- TTL、衰减、过期、归档、遗忘、恢复、备份和可验证的前向 Migration。
- Episode、Trace、Policy、Procedure 与确定性 Observation。
- REST、CLI、MCP 和 Hermes，共用应用服务而不复制业务逻辑。
- `sqlite_scan` 默认向量后端和可选 `sqlite_vec` 投影。
- 受治理的 Provider 插件内核。
- 自动关系发现 Beta：显式启用、预算受控、只生成 Proposal。
- 图片证据 experimental preview：显式安装、显式启用、宿主执行全部输入安全校验。
- 稳定的 `hl-mem eval` 能力继续随 wheel 发布；研究和历史实验放在顶层 `benchmarks/`。
- Python 3.12、3.13、3.14 支持矩阵。

### 2.2 不做

- PostgreSQL 探针或 PostgreSQL 存储后端。
- 独立 Tag 候选通道；保留 Tag 分类和 soft boost。
- Extraction pre-filter 生产功能面。
- LLM 直接裁决冲突、直接将 Claim 标记为 `disputed` 或直接执行 `supersede`。
- 自动应用关系发现结果。
- 默认自动执行 Query Expansion、Resurrection、LLM dedup、LLM reclassify、Policy 归纳或语义冲突 consolidation。
- 默认离线模式、低质量本地替代、Fake Provider 生产回退或静默降级。
- Neo4j、Graphiti、Graph 数据库双写、Text2Cypher、无限图遍历或完整 GraphRAG。
- pluggy、任意 Hook、插件路由、插件 Migration、插件后台任务、插件存储后端或插件覆盖安全策略。
- 全仓目录重排、DI 框架、通用 `utils/common/services` 垃圾桶目录和机械代码行数限制。
- 10 万或 100 万数据量的 1.0 发布阻断门；规模测试提供报告，不承诺企业容量。
- Down Migration。回滚通过升级前备份恢复，不在已升级数据库上逆向执行 Schema。

## 3. 目标代码架构

保留现有模块化单体，只增加必要目录并整理真实热点：

```text
src/hl_mem/
├─ api/
│  ├─ routes/              # memory、recall、experience、maintenance 路由
│  ├─ conflict_routes.py   # 现有冲突案卷与 delegation 路由
│  ├─ schemas.py           # 传输 DTO
│  └─ server.py            # FastAPI 工厂、中间件和路由组装
├─ application/            # 摄入、召回、冲突、生命周期等用例与事务边界
├─ config/
│  ├─ models.py            # 按领域组合的类型化配置
│  ├─ loader.py            # TOML、环境密钥与配置版本加载
│  ├─ migrate.py           # 0.x → 1.x 配置迁移
│  └─ secrets.py           # 密钥来源和脱敏规则
├─ domain/                 # 纯领域模型、状态机与业务规则
├─ ingest/
│  ├─ extraction/          # Prompt、Schema、解析、修复和 LLMExtractor 门面
│  ├─ embedder.py
│  └─ image_describer.py
├─ llm/                    # 中立 LLM 请求类型、客户端和内置适配器
├─ plugins/
│  ├─ contracts.py         # 1.x Provider Plugin API
│  ├─ manifest.py          # Manifest 与版本协商
│  ├─ discovery.py         # Entry Points 发现和显式启用
│  ├─ registry.py          # 类型化注册、冲突检查和工厂解析
│  └─ proxies.py           # 预算、审计、重试、指标和安全代理
├─ recall/                 # 检索、融合、排序、关系扩展和 Trace
├─ storage/                # SQLite Repository、Migration、备份和投影
├─ workers/
│  ├─ maintenance.py       # 确定性维护项目与开关
│  ├─ job_handlers.py      # 执行阶段二次门控与 Job 分派
│  └─ worker.py            # 租约、轮询、心跳和运行时编排
├─ experience/             # Episode、Trace、Policy 与 Procedure
├─ evaluation/             # 随 wheel 发布的稳定评测能力
├─ observability/          # 审计、模型调用、费用和运行指标
├─ security/               # 安全策略与输入边界
├─ adapters/               # Hermes 等宿主适配
├─ mcp/                    # MCP 传输适配
├─ components.py           # 唯一组件组装入口；不引入 DI 框架
├─ settings.py             # 兼容内部导入的薄门面，不再承载全部实现
├─ protocols.py            # 非插件的内部跨模块协议
└─ errors.py               # 公共错误定义

benchmarks/                # 数据集适配、研究 Runner 和历史实验，不进 wheel
tests/                     # unit、integration、eval、scenario
docs/                      # 当前文档；历史材料进入 archive
```

依赖方向保持为 `interfaces/workers → application → domain`，基础设施通过明确协议注入。`plugins/` 只负责扩展契约和治理，不拥有记忆业务语义；Provider 实现仍放在对应能力模块中。`components.py` 继续作为组合根，改为从 Registry 解析 Provider，不再维护不断增长的 Provider 条件分支。

不按文件长度机械拆分。只有当模块同时承担多种职责、形成循环依赖、难以隔离测试或频繁产生冲突时才拆分。复杂度 Ratchet 不得扩张；每个完成重构的热点同步降低自身预算。

## 4. 配置与默认运行体验

`hl-mem init` 改为服务中立的交互式配置向导：选择并验证 LLM、Embedding 和可选 Reranker，生成带 `schema_version = 1` 的 TOML。缺少被启用 Provider 的密钥、模型或能力时明确失败，不使用 Fake 或低质量替代。

`hl-mem doctor` 收口为只读诊断入口，检查配置版本、密钥存在性、Provider 连通性与能力、数据库和 Migration、插件兼容性、Hermes 契约及备份可恢复证据。诊断不得自动修改配置或数据库。

`hl-mem config migrate` 负责 0.x → 1.x：

1. 读取旧配置并验证来源。
2. 输出确定性变更清单和移除项。
3. 默认 dry-run；`--apply` 前创建原文件备份。
4. 写入新的版本化配置并再次完整校验。
5. 显式 `relation.discovery_mode = "auto"` 转为 `audit`；默认 `off` 不产生迁移动作。
6. Query Expansion 与 Resurrection 的旧 `auto` 转为 `off` 并给出迁移说明。

1.0 运行时不永久接受 0.x 配置别名。测试和开发使用 `Settings.for_test()` 或显式开发配置；删除面向普通用户的 `init --offline` 和生产 Fake 默认值。

配置按 Database、Extraction、Retrieval、Governance、Lifecycle、Integration、Observability 和 Plugins 分组。根 Settings 只负责组合与跨组校验，密钥只来自受支持的环境变量或 `.env`，不得写进 TOML、日志、Trace 或错误正文。

## 5. 受治理的 Provider 插件内核

### 5.1 稳定能力与发现

- Entry Point group 固定为 `hl_mem.providers`。
- 1.x 稳定插件能力为 LLM、Embedding、Reranker。
- Image Describer 是 experimental preview，不享受 1.x 接口稳定承诺。
- 内置 Provider 与第三方 Provider 使用同一 Registry 和宿主代理；内置实现可以直接注册，但不得绕过治理路径。

Manifest 必须提供：

- `id`：全局稳定插件标识。
- `version`：插件版本。
- `api_version`：Plugin API 主版本，1.0 固定为 `1`。
- `requires_hl_mem`：Core 版本约束。
- `capabilities`：Provider 名称、类型和能力声明。
- `config_schema`：插件自有配置 Schema。

外部插件只有出现在 `[plugins].enabled` 中才加载；插件配置仅位于 `[plugins.<id>]`。插件 ID、Provider 名称、能力注册或主版本冲突一律 fail-closed，启动错误必须指明冲突双方，不按发现顺序覆盖。

插件是进程内的受信任扩展，不是安全沙箱。项目只保证通过正式契约发生的调用受治理，无法阻止恶意 Python 包自行访问进程、文件或网络；文档和 `doctor` 必须明确提示只启用可信插件。Allowlist 防止“安装即激活”，不把第三方代码误表述为隔离执行。

### 5.2 宿主代理与安全边界

Registry 不向业务层返回第三方原始对象，只返回宿主管理的 LLM、Embedding、Reranker 或 Image 代理。代理统一实施：

- 超时和有界重试。
- Provider 错误归一化。
- 原子预算预留和结算。
- 调用、Token、批次、延迟和费用审计。
- 密钥与响应正文脱敏。
- 失败隔离和健康状态。

稳定 Provider 契约采用宿主持有传输的 Adapter 模式：插件声明能力、构造中立 `ProviderRequest` 并解析 `ProviderResponse`，HTTP 客户端、批处理边界、重试和用量结算由宿主执行。稳定插件不得在 Provider 方法内部隐藏额外网络重试。现有 Embedding 和 Reranker 的直接 HTTP 实现必须先迁入宿主 Client，再作为稳定扩展点发布；无法满足该契约的 SDK 适配不进入 1.0 稳定插件面。

Image preview 在调用插件前由宿主 `ImageInputGuard` 完成协议、DNS/IP、SSRF、重定向、文件根目录、MIME/Magic、大小和哈希校验。插件只收到已经物化并验证的图片描述对象，不能访问未经验证的 URI 或任意本地路径。

内置迁移使用逐能力等价门禁：同一输入在旧工厂和 Registry 路径下必须保持请求语义、响应归一化、错误分类、重试、指标与审计一致。三种稳定能力分别迁移和提交，不一次性替换 `components.py` 全部工厂。

## 6. 统一模型用量与费用治理

当前 LLM、Embedding、Reranker 和 Image Describer 是四条独立网络路径。1.0 建立单一 `UsageGovernor`，通过对应宿主代理覆盖：

- `LLMClient.complete()` 的实际 Provider 请求。
- Embedder 每个实际 `_request()` 批次，而不是同时在 `embed()` 门面重复计量。
- 每次 Reranker 网络请求。
- 每次 Image Describer 网络请求。

预算协议为原子 `reserve → settle/release`：

1. `reserve` 在独立 usage SQLite sidecar 中以 `BEGIN IMMEDIATE` 同时检查已用量、未结算预留和上限，并写入唯一 reservation。
2. 请求成功后按 Provider 返回的实际用量 `settle`；实际用量超过预留时照实记账并阻止后续透支。
3. 请求确定未发送时 `release`；已发送但结果不明时按预留量保守结算。
4. reservation 带租约和过期时间，崩溃恢复任务只回收能够证明未发送的预留，其余保守结算。

账本记录 capability、operation、plugin/provider、model、请求数、输入/输出 Token、Embedding 项数、Rerank 文档数、图片数、延迟、状态和估算费用。价格由用户配置或随 Provider 适配器版本化提供，不通过网络静默更新。费用估算缺失时仍执行调用量和 Token 门禁，并明确标记金额未知。

## 7. 自动行为矩阵

### 7.1 默认开启

| 能力 | 默认行为 | 边界 |
|---|---|---|
| TTL、过期、衰减、归档、保留期清理 | 自动 | 仅确定性生命周期规则 |
| stale 依赖传播 | 自动 | 只关闭失效派生，不生成新语义 |
| Observation 构建 | 自动 | 确定性、有证据、幂等；历史召回不注入当前 Observation |
| near-copy 审查 | 自动 | 只更新 dedup 审计对，不合并 Claim、不调用 LLM |
| L0 冲突与悬挂引用修复 | 自动 | 只执行冻结的确定性规则 |
| Plan fulfillment | `enforce` | 保持现有稳定、确定性和 abstain 边界 |
| 已显式产生的提取重试、访问和反馈待办 | 自动 | 受幂等、重试和终态约束 |

### 7.2 默认关闭

| 能力 | 1.0 行为 |
|---|---|
| LLM conflict consolidation | 不再每日自动入队；显式运行也只生成审计案卷，禁止直接改变 Claim 状态 |
| LLM dedup | 显式启用并受预算约束；与确定性 near-copy 开关分离 |
| Policy induction | 不自动发布派生 Policy；仅显式运行 |
| LLM reclassify | 不自动入队；仅显式、预算化批处理 |
| Query Expansion | 默认 `off`；显式 `auto` 时受查询预算、超时和 Trace 约束 |
| Resurrection | 默认 `off`；显式启用时仍需执行阶段重新验证 |
| Relation Discovery | 默认 `off`；唯一启用模式为 `audit`，删除 `auto` 正式落边语义 |
| Mental Model 生成 | 不增加自动生成器；仅保留显式、有证据的重建能力 |

每个可关闭的语义任务同时具有入队门控和 Handler 执行阶段二次门控。1.0 Migration 将已关闭类型的 pending Job 置为 `dead` 并记录 `disabled_by_v1_migration`；pending resurrection deferred task 置为 `abandoned`。因此升级前已入队的任务不会在新默认值下继续产生副作用。

## 8. 关系图边界

1.0 维护证据驱动的记忆关联图，不建设 Zep 级自动时序知识图谱：

- SQLite 是 Event、Evidence、Claim、Entity、Relation 的唯一权威存储。
- 确定性实体关联和实体 soft boost 默认开启，不增加独立 LLM 调用。
- 正式关系必须来自确定性规则、人工写入或人工审核通过的 Proposal。
- 关系保留方向、来源、证据、置信度、valid time、recorded time、失效和删除语义。
- 普通召回可以使用低成本实体关联信号；一跳或两跳关系扩展只在关系意图下触发。
- 路径、候选和种子数量有界，Trace 记录关系带来的候选、延迟和最终贡献。
- 关系重建只能显式执行，必须预估工作量、支持幂等和断点续传；默认不触发独立模型调用。

旧 C-series 阈值已失效，不作为 1.0 发布证据。关系功能若要改变默认召回，必须重新冻结语料、v0.36.1 基线、判定脚本和成本记录，并证明净收益；未通过时保持 Beta 显式能力。

## 9. 正确性、安全与迁移修复

### 9.1 双时间召回

Claim 继续按 `as_of` 和 `known_as_of` 过滤。Policy 与 Derivation 当前没有完整历史版本，1.0 不为它们临时伪造双时间：只要请求包含任一历史时间参数，Context Packet 就不注入 Policy 或 Derivation；当前时间召回继续使用活跃项。该语义进入 REST、MCP 和回归测试。

### 9.2 请求体限制

API 请求上限按 ASGI 实际接收字节执行，而不是只信任 `Content-Length`。声明超限、实际流式超限、无长度头超限、非法长度头都返回 413 或明确的 400；中间件不得先将无限请求完整读入内存。REST 传输测试覆盖分块、无头、正常 JSON 和边界值。

### 9.3 SQLite 资源

统一连接所有权：请求连接、Worker 自有连接、Repository 测试和临时数据库均由 Context Manager 或 Fixture 关闭。全量测试必须达到零 `ResourceWarning`，随后在 CI 将 `ResourceWarning` 提升为错误，防止回归。

Phase 1 的门禁覆盖 pytest 生命周期内可观测的 `ResourceWarning`、unraisable warning，以及 API、MCP、Worker 和 `Database` 的确定性关闭；不承诺捕获 pytest 钩子全部结束后、仅在 Python 解释器最终析构时才出现的警告。该边界不阻塞 1.0，后续以独立的 SQLite 生命周期观测专项处理，方案见 `docs/research/sqlite-connection-lifecycle.md`。

### 9.4 配置与数据库回滚

Migration 保持只前进。首次使用 1.0 或 RC 打开生产数据库前：停止写入、创建数据库和 tombstone sidecar 备份、验证 Manifest 与校验和、在副本上完成 Migration 和 `doctor`。回滚使用旧二进制、旧配置备份和升级前数据库快照；不得让旧二进制直接打开已迁移数据库。RC 后产生的新写入不会自动回放到旧快照，此限制必须在升级提示中明确显示。

## 10. 1.x 兼容政策

1.0 发布时以新的 `docs/compatibility.md` 替换仅适用于 0.x 的政策：

- Stable REST、MCP、CLI、配置 Schema、导入导出、备份格式和 Provider Plugin API 在 1.x 内保持向后兼容。
- Stable 契约的删除或不兼容更改只在下一个主版本发生；1.x 可以增加可选字段和能力。
- Beta 与 experimental 契约可以在次版本调整，但必须在 Changelog 给出行为和迁移说明；experimental 不承诺兼容窗口。
- SQLite 内部表不是公共 SQL API；受支持的 Migration 保证应用数据前向升级。
- Plugin API 主版本不匹配、未来配置版本和未来备份格式均明确失败，不进行猜测性兼容。
- 安全修复可以立即收紧输入验证，但不得静默改变已存记忆语义。

## 11. Benchmark、质量与发布门禁

必须满足：

- 全量测试零失败、零 `ResourceWarning`。
- 覆盖率门槛 80%，不得通过排除核心模块达标；以后只允许 Ratchet 上升。
- Python 3.12、3.13、3.14 测试矩阵全绿。
- Ruff、Black、isort、mypy、构建、import boundary 全绿。
- OpenAPI、MCP、Migration 和配置 Schema 快照全绿。
- 空库安装、v0.36.1 历史库升级、备份恢复和重复 Migration 全绿。
- 公共 recall fixture 入库；CI 不允许因 fixture 缺失而跳过召回门禁。
- PR 快速门禁在标准 CI Runner 上不超过 90 秒；完整带覆盖率单元门禁不超过 5 分钟，集成和真实模型评测独立并行或 nightly 运行。
- v0.36.1 与 1.0 RC 使用同一重新冻结的公开 Benchmark 协议；结果、模型、Prompt、配置、数据 fingerprint、Token 和费用完整记录。
- 默认关闭能力不得产生模型请求；所有启用的模型调用必须出现在 usage ledger 和 Trace/Audit 中。
- 插件失败不得破坏未使用该插件的核心摄入和召回；插件冲突必须阻止启动。
- 历史时间查询不得混入当前 Policy 或 Derivation。
- 超限流式请求不得进入业务处理。

安全与开源工程同步完成：`SECURITY.md`、Dependabot、CodeQL、`pip-audit`、发布 SBOM、GitHub Actions 固定提交 SHA、Issue/PR 模板、支持版本矩阵、发布检查单和密钥泄漏检查。不建设企业权限和审批平台。

## 12. 实施顺序

1. 第 1 周：冻结基线；修复 ResourceWarning、历史召回污染、流式请求上限、Python/EOL 和工作区卫生；建立 1.x 兼容政策草案。
2. 第 2 周：收口配置分组、`init`、`doctor`、`config migrate`、生产 fail-fast、备份恢复政策和自动行为迁移。
3. 第 3 周：实现 Provider Plugin API、Manifest、Registry、显式发现和宿主代理；逐个迁移三种稳定内置 Provider；图片只进入 preview。
4. 第 4 周：实现四路径 UsageGovernor；拆分确定性与模型任务开关；取消旧 pending 任务；将 LLM conflict consolidation 收口为 audit-only。
5. 第 5 周：围绕本轮真实改动拆分 LLMExtractor、Worker、Recall enrichment 和 API 路由热点；不做无关搬迁；完成稳定 evaluation 与历史 benchmarks 解耦。
6. 第 6 周：运行完整 Benchmark、性能、安装、Migration、恢复、安全和插件等价验证；修正文档并发布 `1.0.0rc1`。
7. 第 7 周：连续七天观察，只修缺陷。任何 P0/P1、数据语义、Migration 或稳定契约修复都重新开始七天观察；满足全部门禁后发布 `1.0.0`。

## 13. 完成定义

Core 1.0 完成时必须同时具备：

- 默认配置不存在隐藏模型费用、未声明网络调用或高风险自动状态变更。
- 高质量 Provider 未配置时明确失败，测试替身不会进入生产路径。
- 插件扩展真实可用，但只能通过受治理的稳定边界接入。
- SQLite 权威记忆、证据链、双时间 Claim、生命周期和冲突案卷保持一致。
- 自动关系只产生可审计 Proposal，Graph 不引入外部服务和双写成本。
- 配置和数据库升级可预演、可验证、可通过备份恢复。
- 核心模块职责清楚，新增抽象都有两个使用者：内置实现与第三方/测试契约，或两个实际业务路径。
- 当前文档、默认值、能力矩阵、API/MCP 快照和实际代码行为一致。
- 仓库不保留失效生产入口、假支持、无调用方兼容层或无法说明用途的文件。

达到上述条件后，HL-Mem 1.0 才被视为一个功能强、边界清晰、可扩展且维护成本合理的精致项目。
