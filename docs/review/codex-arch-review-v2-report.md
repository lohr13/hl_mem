# hl_mem v0.4.3 架构复杂度审查报告

## 总体评估

- 复杂度评分: **6.7/10**（1=极简，10=过度复杂）
- 核心判断: 分层方向总体正确，但 Phase 13/14 的迁移态兼容层、三套配置入口和只覆盖部分调用方的 LLM 抽象，使一个本地单 Agent 系统承担了偏高的导航与变更成本。
- 审查范围: 已逐行阅读 `src/hl_mem/` 下全部 **77 个 Python 文件（8,164 行）**；另对测试目录做了静态耦合检索。按约束未运行 pytest。
- P0 结论: **无 P0**。当前问题主要是可维护性和扩展成本，不构成立即的数据安全、事务完整性或架构不可逆风险。

## 维度评分

| 维度 | 评分 | 说明 |
|------|------|------|
| 1. 抽象层次 | 6.5/10 | Application Service 和 Provider 边界有价值，但兼容 wrapper、仅部分落地的 Protocol/Provider，以及双重构造参数增加了间接性。 |
| 2. 模块划分 | 7.0/10 | `api/application/domain/storage` 主干清楚；写入逻辑仍位于 `recall/`，且多个 300～500 行编排模块职责过密。 |
| 3. 依赖关系 | 6.5/10 | 未发现现实循环导入，但迁移脚本反向依赖可变业务模块，`repository.py` 与 `config.py` 扇入偏高。 |
| 4. 数据模型一致性 | 7.0/10 | API/LLM 边界已有 Pydantic，但内部仍以 `dict[str, Any]` 和 JSON 字符串穿透各层，配置来源也不唯一。 |
| 5. 可维护性 | 7.0/10 | 简单改动常需跨 API→Application→Recall→Repository；测试存在私有成员和兼容路径耦合，部分“为什么”文档良好但分布不均。 |
| 6. 扩展空间 | 6.0/10 | 新 Provider 和一跳关系均有扩展点；新记忆类型与真正的命名空间隔离仍需横切修改。 |

## P0 问题（严重）

无。以本地单 Agent、SQLite 首版的实际规模衡量，没有发现必须在继续开发前立即修复的架构复杂度问题。

## P1 问题（重要）

### P1-1: 配置存在三套真相源，组件构造逻辑已经发生重复

- 位置: `src/hl_mem/settings.py:13-63`、`src/hl_mem/config.py:7-40`、`src/hl_mem/components.py:37-181`、`src/hl_mem/api/server.py:46-75`、`src/hl_mem/workers/worker.py:71-83`
- 现象: `Settings.from_env()`、导入时求值的 `config.py` 常量、各工厂内部的 `os.getenv()` 同时管理相同参数。更明显的是 reranker 构造在 `components.make_reranker()` 与 `api.server._make_reranker()` 中重复；API 创建了 `Settings`，但构造 embedder/reranker 时没有把该快照传给工厂。Worker 又混用 `config dict`、模块常量和环境变量。
- 影响: 同一进程中可能同时存在“启动快照值”和“模块导入值”；新增配置必须在多个位置同步默认值、校验和健康检查。测试或嵌入式启动改变环境变量时，行为尤其难推断。
- 建议: 选 `Settings` 为唯一非敏感配置对象；启动时只解析一次，将其显式注入 `make_embedder/make_reranker/make_extractor`、`Worker` 和 `McpMemoryServer`。`config.py` 只保留真正不随部署变化的领域常量；删除 `api.server._make_reranker()`，统一调用组件工厂。

### P1-2: 写入领域逻辑错放在 `recall/`，迁移脚本还依赖了声称“冻结”的可变实现

- 位置: `src/hl_mem/recall/__init__.py:1-5`、`src/hl_mem/application/ingest.py:16-24`、`src/hl_mem/recall/attribute_map.py:1-298`、`src/hl_mem/recall/conflict.py:21-137`、`src/hl_mem/recall/dedup.py:30-99`、`src/hl_mem/storage/migrations/backfill_conflict_key_v2.py:1-14`
- 现象: 包自身已承认 `attribute_map/conflict/dedup` 属于写入路径，却仍留在 `recall/`；因此 ingest 与 migration 都反向依赖 recall。更严重的是 migration 顶部写着算法已快照、不得随业务改变，但实际直接导入 `recall.attribute_map` 和 `recall.conflict` 的当前函数。
- 影响: 导航名称与真实职责不一致；修改召回包可能改变历史数据回填结果，破坏 migration 的不可变性假设。未来拆包时，迁移代码会成为隐蔽耦合点。
- 建议: 将当前算法移动到 `domain/claims/attributes.py`、`domain/claims/conflicts.py`、`domain/claims/dedup.py`；旧 `recall.*` 暂时只做明确弃用的 re-export。迁移文件必须内联或导入独立的 `migration_snapshots/v006.py` 快照实现，不再依赖活跃领域函数。

### P1-3: 三条核心路径由大型编排函数承担过多职责

- 位置: `src/hl_mem/application/ingest.py:155-359`、`src/hl_mem/recall/recall_pipeline.py:96-351`、`src/hl_mem/workers/worker.py:106-182`、`src/hl_mem/workers/worker.py:184-300`
- 现象: `store_extracted()` 同时完成规范化、TTL、向量化、精确去重、语义去重、冲突处理、状态迁移、证据写入、审计和事务；`hybrid_claims()` 同时处理两路检索、兼容降级、可见性、RRF、多因子排序、关系扩展、rerank、trace 和审计；Worker 同时负责调度、维护、七类 job 分派和事件提取。
- 影响: 新增排序通道、冲突策略或 job 类型都要进入高分支密度函数，局部改动难以隔离验证；新人必须一次理解存储、领域和可观测性细节。
- 建议: 不增加新框架，只提取同文件内或同包内的纯阶段函数：写入拆为 `build_claim → find_resolution → persist_resolution`；召回拆为 `collect_candidates → filter_and_score → expand → rerank → finalize`；Worker 用 `dict[job_type, handler]` 注册处理器，并把维护循环收敛为 `_run_maintenance(now)`。Application Service 继续持有事务边界。

### P1-4: LLM Provider 抽象只完成了提取路径，形成新旧两套调用方式

- 位置: `src/hl_mem/llm/client.py:21-112`、`src/hl_mem/llm/providers.py:17-81`、`src/hl_mem/ingest/llm_extractor.py:131-169`、`src/hl_mem/workers/consolidate.py:51-119`、`src/hl_mem/workers/reclassify.py:35-43`
- 现象: `LLMClient → Provider` 对 structured output 能力与降级确有价值，并非过度抽象；但只有 `LLMExtractor` 使用它。`LLMConflictJudge` 仍自行拼 HTTP、重试和解析；`reclassify.classify_batch()` 还调用 `extractor._post()`，而当前 `LLMExtractor` 已没有该方法。`LLMExtractor` 构造器同时接收旧 transport 参数和可选 `llm_client`，产生双重状态。
- 影响: 新增 Ollama 等 Provider 时，提取可复用 Provider，冲突归并仍需另改；超时、重试、结构化输出和错误语义继续分叉。已删除私有 API 的残留调用说明迁移边界不完整。
- 建议: 保留 `LLMClient/Provider`，让 `LLMConflictJudge` 和 reclassify 直接消费 `LLMClient` 与各自的 `StructuredOutputSpec`。完成迁移后，将 `LLMExtractor.__init__` 收敛为 `llm_client + extraction_policy`，旧签名仅在一个显式 legacy factory 中保留一个版本周期。

### P1-5: 内部数据契约以 `dict/Any/JSON` 贯穿，Pydantic 只保护了入口

- 位置: `src/hl_mem/api/server.py:153-159`、`src/hl_mem/application/ingest.py:65-103`、`src/hl_mem/application/ingest.py:190-217`、`src/hl_mem/application/recall.py:211-233`、`src/hl_mem/experience/service.py:185-238`、`src/hl_mem/storage/repository.py:91-200`
- 现象: API 将 Pydantic `model_dump()` 成 dict，应用层再拼数据库 dict；Claim 的 `value_json/qualifiers_json` 在提取、冲突、召回、Experience 与 Worker 中反复 `json.dumps/json.loads`。大量服务和 Protocol 使用 `Any`，字段约束依赖调用方默契。
- 影响: 字段改名或新增记忆类型无法由类型检查定位影响面；格式转换散落导致状态 envelope、原始值、响应文本三种形态并存。Pydantic 的严格性本身不是问题，问题是边界之后契约立即消失。
- 建议: 不把 ORM/Pydantic 推入所有层；只增加少量稳定 dataclass/TypedDict：`StoredEvent`、`ClaimDraft`、`StoredClaim`、`RecallResult`、`FeedbackRecord`。JSON 编解码集中到 Repository 边界，应用/领域层操作 Python 值。

### P1-6: `repository.py` 成为存储黑洞，且 Application Service 仍直接写 SQL

- 位置: `src/hl_mem/storage/repository.py:1-518`、`src/hl_mem/application/ingest.py:229-354`、`src/hl_mem/application/recall.py:113-125`、`src/hl_mem/application/recall.py:266-302`、`src/hl_mem/experience/service.py:52-365`
- 现象: 单文件集合 Event、Claim、Evidence、Job、Derivation 五类 repository；同时应用层为了事务和批量查询绕过 repository 直接 SQL。`ExperienceService` 又兼任 Episode/Trace/Feedback/Policy 的服务与持久化层。
- 影响: 存储 schema 变化既可能改 repository，也可能遗漏 application/worker 中的裸 SQL；“所有持久化都在 storage”这一架构承诺并未完全成立。
- 建议: 按聚合拆成 `storage/claims.py`、`events.py`、`jobs.py`、`evidence.py`、`experience.py`，共享 `_insert` 与批量工具；为应用层当前的批量 rivals、derivation 和冲突写入补仓储方法。不要为每条 SQL 建接口，只迁移会跨层复用或承载领域不变量的操作。

## P2 问题（建议）

### P2-1: 兼容 re-export 和 no-op 延长了迁移态

- 位置: `src/hl_mem/api/pipeline.py:1-13`、`src/hl_mem/recall/router.py:1-7`、`src/hl_mem/recall/policy.py:8`、`src/hl_mem/ingest/embeddings.py:10`
- 现象: 多个旧路径仅转发；`api.pipeline._build_observation()` 甚至只为兼容 monkeypatch 保留 no-op。生产代码 `api.server` 仍从兼容层导入 `new_id`。
- 影响: 搜索符号时会出现真假两个入口，测试继续固化旧模块边界，迁移无法自然结束。
- 建议: 先让 `src/` 全部改用最终路径，再在一个明确版本窗口内保留带 `DeprecationWarning` 的 re-export；删除 no-op，并把兼容性测试集中到单独文件。

### P2-2: 测试对私有成员和模块内部补丁存在可见耦合

- 位置: `tests/unit/test_comprehensive_fixes.py:94-157`、`tests/unit/test_pipeline.py:1-35`、`tests/unit/test_hybrid_priors.py:81-87`、`tests/unit/test_provider.py:77-176`、`tests/unit/test_decay.py:113-115`
- 现象: 测试直接 monkeypatch `server._queue_event`、调用 `_make_*`、`Worker._dispatch`、`LLMExtractor._claim`，并检查 Hermes provider 的 `_failure_count/_circuit_open_until`；若干测试仍导入 `hl_mem.api.pipeline`。
- 影响: 合理的内部拆分也会造成大量测试噪声，反过来推动兼容 no-op 和旧导入长期存在。
- 建议: 对组件工厂、job handler、claim normalizer 和 circuit breaker 提供窄的公开测试边界；HTTP 测试继续注入 client，不再 patch 全局 `httpx.post`。只保留少量白盒测试验证算法纯函数。

### P2-3: Protocol 的采用不一致，部分属于提前抽象

- 位置: `src/hl_mem/protocols.py:8-32`、`src/hl_mem/storage/base.py:8-15`、`src/hl_mem/application/recall.py:30-40`、`src/hl_mem/components.py:37-101`
- 现象: Embedder/Reranker 已有真实与 fake 实现，Protocol 有用；Extractor 也有两个实现，但签名并不完全一致。`StorageDatabase` 只有 PostgreSQL 边界的最小声明，实际应用仍硬依赖 SQLite connection/SQL，未被消费。工厂返回 `Any` 又削弱了 Protocol 的收益。
- 影响: 读者看到接口会误判后端已可替换；静态类型无法帮助检查工厂输出。
- 建议: 工厂分别返回 `EmbedderProtocol`、`RerankerProtocol | None`、`ExtractorProtocol`；统一 Extractor 的 `context` 参数。若近期不实现 Repository 级 PostgreSQL 适配，删除未使用的 `StorageDatabase`，把 `PostgresDatabase` 明确标为实验性连接探针。

### P2-4: Hermes provider 是适配层中的上帝类

- 位置: `src/hl_mem/adapters/hermes/provider.py:11-358`
- 现象: 一个类同时承担同步/异步 HTTP、熔断、后台预取缓存、事件转换、Episode/Trace 推导和 Hermes 生命周期 hook。
- 影响: Hermes API 变化、熔断策略变化和 Episode 规则变化互相影响；大量宽泛异常捕获使失败语义难定位。
- 建议: 保持对外 `HLMemProvider` 不变，内部组合三个小对象：`HLMemHttpClient`、`PrefetchCache`、`EpisodeMapper`。这是局部拆分，不需要引入通用插件框架。

### P2-5: Pydantic 提取 schema 的严格性合理，但“严格后补默认”语义矛盾

- 位置: `src/hl_mem/ingest/schemas.py:10-35`、`src/hl_mem/ingest/llm_extractor.py:288-303`、`src/hl_mem/ingest/llm_extractor.py:342-379`
- 现象: `extra="forbid"`、枚举和范围验证适合不可信 LLM 输出；但校验前 `_parse_legacy_defaults()` 会补齐包括空 `value` 在内的全部必填字段，之后再由严格 schema 拒绝部分补值。
- 影响: “新协议必须严格”与“旧模型可兼容”的边界不清晰，schema retry 的错误信息可能来自兼容填充而非原始响应。
- 建议: 将 legacy 兼容做成显式版本 adapter，只补历史上确实可安全推导的字段；先记录原始缺失字段，再验证。新 Provider 默认走严格路径。

### P2-6: namespace/tenant 目前只是局部字段，不是端到端隔离

- 位置: `src/hl_mem/api/schemas.py:18-25`、`src/hl_mem/api/server.py:185-202`、`src/hl_mem/application/ingest.py:123-130`、`src/hl_mem/workers/worker.py:119-138`、`src/hl_mem/workers/induce_policies.py:49-53`
- 现象: Event 接受 `tenant_id`，Recall 接受独立的 `namespace`，但 API 审计写死 `tenant_id="default"`；显式记忆、保留清理、策略归纳和维护任务多处写死 default。
- 影响: 对当前本地单 Agent 场景不是缺陷；但如果未来宣称支持多租户，仅补鉴权或查询参数会遗漏后台任务和派生数据。
- 建议: 当前只需文档明确“单租户”；若启动多租户项目，再引入统一 `NamespaceContext` 并强制所有 repository 方法接收 namespace，同时为 job payload、derivation、policy、audit 建一致隔离键。

### P2-7: 一跳关系扩展的数据结构可复用，但算法写死一跳

- 位置: `src/hl_mem/recall/relation_expansion.py:14-36`、`src/hl_mem/recall/relation_expansion.py:46-108`、`src/hl_mem/domain/relations.py:74-153`
- 现象: 配置已有 seed/candidate/weight/allowed relation，边查询也支持批量；但 `ExpandedCandidate` 只存 `seed_id + 单边`，trace 也只记录一条 edge，没有 depth、完整 path、visited 或累计衰减。
- 影响: 升级多跳时不能只改配置，必须改候选模型、遍历算法、去环和 trace；不过现有批量边接口可作为基础。
- 建议: 将 `ExpandedCandidate` 扩展为 `path: tuple[RelationHop, ...]`，用有界 best-first/BFS，加入 `max_depth`、visited、每跳衰减和总扩展预算。默认 `max_depth=1` 可保持当前行为，平滑迁移。

### P2-8: 少量魔数和策略仍散落在实现中

- 位置: `src/hl_mem/application/recall.py:107`、`src/hl_mem/recall/recall_pipeline.py:115-116`、`src/hl_mem/recall/recall_pipeline.py:170-180`、`src/hl_mem/adapters/hermes/provider.py:19-23`、`src/hl_mem/workers/induce_policies.py:18-45`
- 现象: packed context 默认 2000、候选下限 50、RRF 常量 60、偏好加权 0.12、Hermes 熔断阈值/窗口、策略归纳 7 天/3 次支持等仍在实现中。
- 影响: 做 A/B 或调优时需要修改源码；`config.py` 声称“所有 magic number 都在此定义”与实际不符。
- 建议: 只集中需要运维或实验调整的策略值到 `Settings`/策略 dataclass；纯算法常量放所属模块并命名，不必把每个数字都变成环境变量。

## 设计亮点（做得好的地方）

- `application/ingest.py:229-357` 将 claim 插入、状态变更、supersede 与 evidence link 放在同一 `BEGIN IMMEDIATE` 中，事务边界明确；审计事件延迟到提交后发出，避免记录未提交状态。
- `domain/temporal.py:18-51` 将双时间可见性集中为纯函数，`repository.py` 与召回管线复用同一判定；这是值得保留的领域抽象。
- `llm/client.py:43-112` 把结构化输出能力、strict 降级和 transport retry 从提取业务中分离。对三个 Provider 与未来 Ollama/OpenAI-compatible 接入而言，这一层有实际价值。
- `ingest/chunking.py:84-234` 的结构检测、稳定分块和截断后二分是内聚的纯逻辑；没有把分块状态持久化，复杂度与长输入需求匹配。
- `recall/trace.py:9-137` 使用有上限、无正文的结构化 trace，兼顾可解释性与敏感数据控制；debug 关闭时不创建 tracer。
- `recall/relation_expansion.py:46-108` 将关系扩展置于明确预算和低权重下，并再次执行 namespace 与双时间可见性过滤，首版一跳实现是克制的。
- `storage/database.py:71-85` 的请求级连接上下文会回滚残留事务再归还池；`repository.py:421-468` 的 job lease token 与 CAS 降低了多进程误完成风险。
- `api/schemas.py:13-87` 与 `ingest/schemas.py:10-35` 对外部输入设定长度、范围和枚举约束；严格验证本身没有造成不合理的灵活性损失。
- `recall/__init__.py:1-5` 主动记录了已知边界债务，说明设计者知道迁移目标；问题在于需要给这项债务一个退场版本。

## 扩展性分析

- **新 Provider（如 Ollama）**:
  - 若 Ollama 暴露标准 OpenAI-compatible API，当前可通过 `openai_compatible + LLM_BASE_URL` 使用，理论上不需要新增 Provider 类。
  - 若要把 `ollama` 作为正式 provider 名称，提取路径至少改 **3 个源码文件**：`llm/providers.py`（能力/差异适配）、`components.py`（注册与错误信息）、`settings.py`（允许值校验）。
  - 若要求冲突归并也统一遵循该 Provider，则还需改 `workers/consolidate.py`，即 **4 个源码文件**。完成 P1-4 后可收敛为“新增 adapter + 注册表”两处。
- **新记忆类型（如 episodic memory）**:
  - 项目已有 `episodes/traces/policies`，因此“新增 Episode API”不必从 Claim 继承；但要让 episodic memory 进入统一召回，需要修改 migration/schema、Experience 存储、召回候选融合、ranking/trace、结果组装、feedback 与 context packing，属于侵入式横切改动。
  - 推荐先定义统一的 `MemoryCandidate` 只承载 `id/type/text/score/namespace/time metadata`，各类型 adapter 产出候选后再融合；不要把 Episode 强行塞入 Claim 表。
- **多租户隔离**:
  - 当前主要阻碍不是 SQLite，而是 namespace 没有成为强制上下文：API 同时存在 `tenant_id` 与 `namespace`，后台维护和策略归纳写死 default，部分表/查询没有 namespace。
  - 对本地单 Agent 无需现在实现；若未来启动多租户，应作为独立架构项目处理，不能以零散 WHERE 条件补丁完成。
- **多跳关系**:
  - 批量边查询、候选预算、允许关系集合与 trace 已提供良好起点；但当前候选模型只表达一跳。
  - 引入 path、depth、visited、累计衰减和全局预算后，可令 `max_depth=1` 保持兼容，再逐步开放 2～3 跳。无需改写主召回架构，但必须替换 `expand_related_claims()` 的内部算法和 trace path 模型。

## 建议的简化顺序

1. **先统一配置与工厂（P1-1）**：低侵入、立即减少重复和环境差异。
2. **完成 LLM 调用迁移（P1-4）**：消除两套 transport，并修正 reclassify 对已不存在私有 API 的依赖。
3. **纠正写入领域模块位置与 migration 快照（P1-2）**：先保护历史回填确定性，再清理兼容导入。
4. **拆分三个大型编排函数（P1-3）**：只提取阶段函数，不引入新的框架或 DI 容器。
5. **最后收紧内部类型与存储边界（P1-5/P1-6）**：围绕稳定聚合逐步替换 `dict/Any`，避免一次性大重写。
