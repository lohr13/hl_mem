# HL-Mem v0.6.0 死代码与精简度审查报告

审查日期：2026-07-24

## 结论摘要

本次逐一读取了 `src/hl_mem/` 下全部 95 个 Python 文件，并检查了实际数据库、REST/Hermes/MCP
调用链、后台任务类型、配置引用和全仓符号引用。

- 当前源码实际为 **9,531 个物理行**，任务基准为 9,543 行，两者相差 12 行。
- 以当前本地单 Agent 的实际运行形态计算，估计 **1,180 行可以安全删除**，约占当前源码
  **12.4%**（按任务基准 9,543 行则为 12.4%）。
- 另有约 **430 行属于“先停用并观察，再删除”**：主要是没有生产入口的 MCP、默认关闭且
  无关系数据的 Relation Expansion，以及只有写入、几乎没有消费的显式反馈/审计附属面。
- 代码精简度评分：**6/10**。核心 ingest/recall/worker 是真实运行代码，但兼容层、未接线接口、
  实验探针、纯透传阶段和“为架构完整而存在”的管理面明显偏多。

最重要的判断不是“某模块设计得是否合理”，而是它在当前系统中是否有运行入口和数据证据。
Experience 与 Mental Models 有真实数据，不能按僵尸功能删除；MCP、PostgreSQL、独立
`memory_relations` 写入面则没有同等证据。

## 审查方法与口径

1. 对 95 个 `.py` 文件逐文件读取，物理行合计 9,531。
2. 对 `var/hl_mem.db` 的所有业务表执行 `count(*)`，并按 job type、claim status、episode
   status、derivation kind/status 进一步分组。
3. 搜索 `adapters/hermes/` 和 `mcp/` 的 HTTP/服务调用，区分“测试调用”“文档提及”和
   “运行时代码调用”。
4. 用 AST 提取函数、类和方法，再做全仓标识符引用计数；对反射、框架装饰器和 Hermes hook
   做人工复核，避免把框架入口误报为死代码。
5. “安全删除”指不损失当前仓库可证实的实际运行功能；纯测试覆盖不算生产用途。未知的仓库外
   调用无法由静态检查证明，因此公共 API 的判断会明确标注风险。
6. 未运行 pytest，未修改任何 `src/` 文件。

## 实际数据库证据

| 业务表 | 行数 | 判断 |
|---|---:|---|
| `events` | 4,132 | 核心在用 |
| `claims` | 1,001 | 核心在用；522 active、314 expired、165 superseded |
| `evidence_links` | 2,092 | 核心证据链在用 |
| `jobs` | 1,383 | 核心在用 |
| `audit_log` | 24,309 | 大量写入 |
| `audit_review` | 0 | 没有审核消费证据 |
| `conflict_cases` | 17 | 冲突终态功能有数据 |
| `consolidation_pairs` | 186 | 语义归并确实运行 |
| `derivations` | 62 | 44 active、18 stale，全部为 observation |
| `episodes` | 59 | 57 success、2 running，确实在用 |
| `traces` | 5,179 | Hermes Episode 同步大量使用 |
| `policies` | 3 | 3 active，策略归纳有产物 |
| `retrieval_feedback` | 1,315 | 绝大多数只是自动曝光记录；仅 2 行有显式 helpful |
| `memory_relations` | 0 | 独立关系写入功能没有实际数据 |
| `schema_migrations` | 17 | 001-015 及两个 data migration 均执行过 |

Job 实际类型只有：

| job type | 状态 | 行数 |
|---|---|---:|
| `extract_event` | succeeded | 1,377 |
| `consolidate_conflicts` | succeeded | 3 |
| `induce_policies` | succeeded | 2 |
| `reclassify_claims` | succeeded | 1 |

`expire_ttl`、`decay_access`、`purge_retention` 和 `retry_failed` 从未以 job 形式出现。前三者中
TTL/decay/purge 仍由 maintenance 直接执行，只有 job handler 是冗余；`retry_failed` 则连
实际 `failed` 状态都与当前 `JobRepository.fail_job()` 的 pending/dead 状态机不一致。

## REST 端点与实际调用方

Hermes 运行时代码实际调用：

- `GET /healthz`
- `POST /v1/events`
- `POST /v1/recall`
- `POST /v1/memories`
- `POST /v1/episodes`
- `POST /v1/episodes/{id}/traces`
- `PATCH /v1/episodes/{id}`

没有任何 `adapters/hermes/` 或 `mcp/` 运行时代码调用：

- `POST /v1/feedback`
- `GET /v1/episodes`
- `GET /v1/episodes/{id}`
- `GET /v1/policies`
- `DELETE /v1/memories/{id}`
- `GET /v1/stats`
- `GET /v1/jobs`

这些端点有测试或 README 记录，但“测试能调用”不是实际调用方。建议保留 `DELETE` 作为显式
遗忘管理能力；其余只读管理端点若没有仓库外客户端，可删除约 45 行。`POST /v1/feedback`
只产生了 2 条显式反馈，价值明显低于其全链路复杂度，见后文。

## 可立即删除的死代码

### 1. 已过删除版本的兼容入口

下列文件明确写着“将在 v0.6.0 删除”，当前版本已经是 0.6.0：

- `src/hl_mem/api/pipeline.py`：17 行。
- `src/hl_mem/ingest/embeddings.py`：12 行。
- `src/hl_mem/recall/attribute_map.py`：12 行。
- `src/hl_mem/recall/conflict.py`：12 行。
- `src/hl_mem/recall/dedup.py`：12 行。
- `src/hl_mem/recall/policy.py`：12 行。
- `src/hl_mem/recall/router.py`：16 行。

合计 **93 行**。除 `storage.repository` 外，生产源码没有依赖这些旧入口。删除相应兼容测试即可。

`src/hl_mem/storage/repository.py:1-25` 仍被大量新源码自己导入，这说明迁移只完成了一半：先把
内部 import 改到 `storage.claims/events/evidence/jobs`，再删除该 25 行 re-export。该工作是
机械替换，不改变功能。

### 2. 明确未调用的函数、类和数据类型

以下标识符在生产代码中只有定义，没有调用；部分仅被测试覆盖：

- `application/ingest.py:429` `_link_event_atomically()`：旧事务实现，已被单事务写入替代。
- `components.py:120` `make_extractor_for_type()`：Worker 自己处理 explicit memory，没有调用该注册表。
- `components.py:128-145` 三个 `make_*_for_test` 及三个别名：测试专用胶水不应放在生产包。
- `core/vector.py:9` `encode_vector()`：生产使用 `ingest.embedder.pack_vector()`，形成重复实现。
- `domain/content.py:8` `ContentPart` Protocol：运行时直接使用两个具体类，没有协议消费者。
- `domain/relations.py:23` `add_relation()` 和 `:50` `get_relations()`：无生产调用，表也为 0。
- `domain/types.py:21` `ClaimDraft`、`:52` `RecallResult`、`:61` `FeedbackRecord`：从未实例化。
- `storage/claims.py:53` `get_stored_claim()`、`:102` `find_active()`、`:333`
  `search_visible()`、`:351` `retract()`：无生产调用。
- `storage/events.py:51` `get_stored_event()`、`:80` `search_events_fts()`：无生产调用。
- `storage/evidence.py:45` `get_links_for_evidence()`、`:59` `insert_observation()`、`:63`
  `get_observation()`：无生产调用。
- `storage/jobs.py:76` `force_finish_job()`：无管理入口。
- `security/retention.py:8` `enforce_event_quota()`：没有写入路径调用。

建议连同只为上述入口服务的 `StoredEvent/StoredClaim` 一起删除。保守估计可删 **约 190 行**。

### 3. 纯透传的“假管线阶段”

`recall/recall_pipeline.py:494-511` 的 `_filter_and_score()`、`_expand_related()`、`_rerank()`、
`_finalize()` 全部原样返回参数。真正工作全部发生在 `_collect_candidates()` 中。它们没有形成
可替换边界，只是让调用栈看起来像管线。将 `hybrid_claims()` 直接返回 `_collect_candidates()`
可删约 **22 行**。

### 4. 实验性 PostgreSQL 探针

`storage/postgres.py:1-33` 只会 `psycopg.connect()`，没有任何 HL-Mem repository 语义、工厂
入口、配置入口或生产调用。它既不能替代 SQLite，也不能验证业务兼容性。唯一调用来自测试。

结论：删除整个文件及边界测试，**33 行**。未来真正做 PostgreSQL 时，应从 repository 契约和
迁移方案重新设计，而不是保留一个连接探针。

### 5. 无生产入口的 MCP 外壳

`mcp/server.py:1-114` 没有 MCP SDK、stdio/SSE transport、CLI entry point 或应用注册代码。
全仓只有单元测试直接实例化 `McpMemoryServer`。所以它不是 MCP Server，只是四个服务方法的
第二层分派器。

结论：如果没有仓库外嵌入方，删除 `mcp/` 的 **115 行**。若确有外部嵌入方，应先补出明确的
启动入口；在此之前不应把它算作已交付功能。

### 6. 未使用的辅助模块

- `recall/extended_pipeline.py` 的 RRF 和 `budget_pack` 只在测试中调用；正式召回各有另一套实现。
  删除 **39 行**。
- `storage/backup.py` 没有 CLI、worker、API 或脚本调用，只有测试。对本地系统备份很有价值，
  但当前属于“实现了却不可用”。若本版不增加真实入口，删除 **50 行**。
- `storage/base.py` 是 2 行空命名空间，直接删除。
- `config.py` 中 `WORKER_MAINTENANCE_INTERVAL`、`WORKER_JOB_LEASE_MINUTES`、
  `WORKER_POLL_INTERVAL` 只有定义；实际使用 `Settings`。删除 3 个常量及注释约 **7 行**。

### 7. Worker 中永远不需要的 job handler

`worker.py:346-353` 的 `expire_ttl`/`decay_access` 与 `:394-409` 的 `purge_retention`/
`retry_failed` 没有任何 enqueue 路径，也没有历史 job 数据。TTL、decay、purge 已由
`_run_maintenance()` 直接调用。

删除四个 handler 和 `JOB_HANDLERS` 项约 **31 行**。特别是 `_handle_retry_failed()` 查询
`status='failed'`，但当前失败任务只会回到 `pending` 或进入 `dead`，正常状态机下永远更新
不到记录。

### 8. 不成立的兼容与防御分支

- `recall_pipeline.py:205-231` 对旧 repository 签名的两组 `except TypeError`，以及
  `hasattr(search_claims_vector/list_embedded/helpful_rates)` 分支，是内部类型已经固定后的历史
  兼容。它们会把真实 TypeError 误当成签名兼容问题。删除约 **25 行**。
- `llm_extractor.py:135-182` 的 `LLMExtractor.from_env()` 已标记 deprecated，正式工厂已经存在。
  删除约 **49 行**。
- `llm_extractor.py:355-394` 的 legacy schema 默认补齐会把不符合当前严格 schema 的旧响应
  悄悄升级；当前 prompt 和 schema 已要求全部字段。若没有旧模型响应兼容承诺，删除约
  **40 行**。
- `adapters/hermes/provider.py:42-59` 对 `_failure_count`、`_circuit_open_until` 的转发属性，
  以及 `:263-265` 三个静态别名只服务旧测试/monkeypatch。删除约 **20 行**。

## 僵尸功能与 YAGNI 评估

### Relation Expansion：建议删除

证据：

- `memory_relations` 为 0 行。
- 默认 `HL_MEM_RELATION_EXPANSION=off`。
- 生产中没有 `add_relation()` 调用，因此没有途径产生独立关系边。
- 当前 `get_relations_batch()` 在默认模式下只返回少数 evidence relation；真正扩展需显式打开。

可删除 `recall/relation_expansion.py` 197 行、`domain/relations.py` 中独立关系 CRUD 和扩展
查询复杂度、Settings/API 接线及 trace path 代码，保守合计 **约 285 行**。Migration 014
已经执行且不可变，不修改；留下空表不影响运行。

### Experience/Episode/Trace/Policy：保留，但缩管理面

它不是摆设：59 episodes、5,179 traces、3 active policies，且 Hermes 会自动创建 episode 和
trace。`induce_policies` 也成功运行过 2 次。

应保留核心写入、奖励回传、策略归纳和 recall policy 注入。可删除没有内部调用方的 REST 列表/
详情端点，或明确将其定位为人工诊断面。不能因为抽象较多就整体删除该通道。

### Mental Models / Observation：保留 Observation，删虚构的其它 kind

62 条 derivation 证明 Observation 在用。`DerivedMemoryMaintainer._KINDS` 还声明了
`mental_model`、`session_summary`，但数据库中两者均为 0，代码也没有 builder。

建议将模块和类命名收窄到 Observation，删除两个未实现 kind 及通用化参数。这里可精简约
**20 行**，但不应删除现有 Observation 链路。

### Feedback：当前几乎是“曝光日志”，不是反馈系统

1,315 行中只有 **2 行 helpful 非空**；`used_by_model` 合计也只有 2。Recall 每次自动批量插入
曝光行，产生存储和写事务，却几乎没有人提交反馈。

建议二选一：

1. 当前版本删除 `POST /v1/feedback`、自动 `_record_feedback()`、helpful_rate 排序因子和相关
   repository 方法，约 **85 行**，并停止继续写入；
2. 若明确要做在线反馈，至少让 Hermes 在会话完成时提交 task outcome，否则不要把曝光记录
   称为反馈。

历史表和 migration 保留不动。

### Observability/Audit：写入在用，消费缺失

24,309 行证明 AuditLogger 不是死代码，但 `audit_review` 为 0，源码也没有查询/审核入口。
此外 `create_app()` 和 `Worker()` 默认均注入 `NullAuditLogger`，说明审计是否启用依赖仓库外
启动代码；当前 CLI/README 没展示该注入。

建议保留核心审计 emit，但删除没有消费者的 review 设想；补一个真实的构造入口，或承认默认
运行不审计并减少大量 best-effort 包装。不能直接删除 233 行 AuditLogger，因为实际库已有
24,309 条记录。

### Security/Retention：保留 purge，删除 quota

`purge_retained_events()` 每轮 maintenance 执行，是实际功能；`enforce_event_quota()` 从未调用，
且单 Agent 本地系统没有租户配额需求。删除 quota 即可。

### Backup：功能有价值但没有交付入口

在线备份算法本身合理，但只有测试调用。对“每行必须有实际用途”的本次口径，它仍是僵尸功能。
建议要么接入 `hl-mem backup/restore` CLI，要么删除，不能继续以“以后可能需要”为由保留。

## 200+ 行文件审查

| 文件 | 行数 | 评价 |
|---|---:|---|
| `recall/recall_pipeline.py` | 524 | 过大；收集、可见性、融合、关系扩展、rerank、trace、audit 全挤在一个函数，且有假阶段 |
| `workers/worker.py` | 465 | 偏大；handler 可拆，但更优先删除无 enqueue 的 handler |
| `ingest/llm_extractor.py` | 461 | 偏大；legacy `from_env` 和 schema 兼容约 90 行可删 |
| `application/ingest.py` | 435 | 职责复杂度基本真实；只发现旧原子 link helper |
| `storage/claims.py` | 389 | 偏大；约四个公共方法无调用，可先删再评估拆分 |
| `storage/experience.py` | 378 | 数据证明功能真实；不建议为了文件长度拆分 |
| `migrations/snapshots/v006_snapshot.py` | 359 | 大但必须保留；不可变 migration 快照不是业务死代码 |
| `workers/consolidate.py` | 337 | 数据证明运行过；复杂度与语义归并基本匹配 |
| `api/server.py` | 332 | 偏大；无内部调用的管理端点可删约 45 行 |
| `domain/claims/attributes.py` | 298 | 大部分是受控映射和规则，实际 ingest/migration 使用 |
| `adapters/hermes/provider.py` | 296 | hook 兼容导致复杂；旧异步/新同步双契约应在外部版本确认后收敛 |
| `application/recall.py` | 286 | 组装证据、反馈、策略、observation 较多；删除反馈后会明显缩小 |
| `ingest/chunking.py` | 234 | 结构化分块复杂度真实，LLM extractor 在用 |
| `observability/audit.py` | 233 | 写入真实，但 health/review 消费链不完整 |

小模块合并方面，`experience/service.py` 只有 16 行继承空壳，可直接让调用方使用
`ExperienceRepository`，或把名称迁到仓储类；`recall/observation.py` 仍有独立 builder 职责，
不建议仅因 51 行而合并。

## 精简预算

以下是保守、可执行的删除预算；行数包含少量调用点调整，但不计测试删除：

| 删除包 | 估计行数 |
|---|---:|
| v0.6.0 已到期兼容模块及 repository re-export | 118 |
| 未调用函数、类型、测试胶水 | 190 |
| PostgreSQL 探针 | 33 |
| 无传输入口的 MCP 外壳 | 115 |
| Relation Expansion 与独立关系写入面 | 285 |
| extended pipeline、空 base、未接线 backup | 91 |
| Recall 假阶段与旧签名兼容 | 47 |
| LLM legacy 构造/schema 兼容 | 89 |
| 无 enqueue 的 worker handlers | 31 |
| 几乎未用的显式 Feedback 链路 | 85 |
| 无内部调用的 REST 管理端点 | 45 |
| 其它转发属性、未使用配置和虚构 derivation kind | 51 |
| **合计** | **1,180** |

这 1,180 行是“当前可证实功能不损失”的估计，不包含：

- 已执行的 migration 和 v006 snapshot；
- 有真实数据的 Experience、Observation、Consolidation；
- 核心 Audit writer；
- retention purge；
- 为确认仓库外 Hermes hook 契约而暂时保留的 provider 公共 hook。

如果先确认没有仓库外 MCP 嵌入方、没有 backup 脚本使用者、也没有 REST 管理客户端，1,180 行
可以作为第一轮删除目标。再观察一个发布周期后，显式反馈和默认关闭功能的删除可把总精简量
推到约 **1,610 行（16.9%）**，但后 430 行不应在没有一次运行期确认的情况下直接删。

## 建议删除顺序

1. 删除已过期兼容入口、PostgreSQL 探针、未调用符号、假管线阶段和无 enqueue handler。
2. 把生产源码从 `storage.repository` 改为具体仓储 import，再删除 re-export。
3. 确认仓库外是否存在 MCP/backup/REST 管理客户端；没有则整体删除。
4. 删除 Relation Expansion 接线，保留不可变 migration 和空历史表。
5. 停止自动写 retrieval exposure；若一个发布周期仍无显式反馈消费，删除反馈排序链路。
6. 保留并收窄 Experience、Observation、Consolidation，因为真实数据证明它们不是摆设。

最终评价：HL-Mem 的核心并非“全是架构摆设”，但最近重构留下了明显的迁移尾巴和提前实现面。
最该做的不是继续拆层，而是删除没有入口、没有数据、只有测试证明其存在的功能。
