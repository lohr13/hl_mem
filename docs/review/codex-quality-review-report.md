# hl_mem v0.6.0 架构品质审查报告

审查日期：2026-07-24

## 总体评价

- 品质评分：**6.5/10**
- 一句话总结：HL-Mem 的核心记忆模型有明确思想且已经产生真实价值，但实现仍处于“设计完成、收敛未完成”的阶段——关键链路认真，系统边缘和跨层契约却保留了明显的迁移尾巴、半成品语义与局部拼接感。

这不是一个没有设计的“屎山”。事件与 Claim 分离、双时间、证据链、冲突终态、混合召回、派生记忆和 Experience
通道之间存在可以解释的总体方向；第一轮数据也证明其中多数核心能力确实在运行。问题在于，系统同时保留了两套
仓储入口、两套数据形态、两种事务所有权、同步/异步两代 Hermes 契约，以及若干“接口已经画出来、运行闭环尚未
形成”的功能。它像一套有能力的工程师连续快速重构后的系统，而不是已经完成最后一轮删改、命名和边界校准的成品。

## 设计语言一致性评估

### 总体判断

设计语言一致性评分：**6/10**。

主干上的分层是成立的：`api` 负责 DTO/HTTP，`application` 负责用例，`domain` 放纯规则，`storage`
处理 SQLite，`workers` 承担异步生命周期。但这个规则只在新代码中大体成立，旧代码和新增功能并没有全部服从它：

- `domain/relations.py:23-71` 直接接收 `sqlite3.Connection`、执行 SQL 并在领域函数中 `commit()`，与
  “domain 是纯领域逻辑”的项目约定正面冲突；关系功能也因此既不像领域模型，也不像仓储。
- `recall/recall_pipeline.py:514-524` 在召回模块中更新派生记忆状态；`experience/service.py:12-13`
  则只是继承仓储的空应用服务。前者把写副作用放进读取层，后者只有应用层名字而没有应用层边界。
- `api/server.py:125-127,261-263` 运行时替换 `IngestService` 的私有 `_queue_event` 方法，而替换进去的
  `_queue_event()` 与服务本身的实现实质相同。这不是依赖注入，而是通过 monkeypatch 绕过边界，违反最小惊讶原则。
- 新代码仍大量从 `storage/repository.py` 的弃用 re-export 导入；与此同时具体仓储文件已经存在。
  这使“仓储已拆分”在结构上成立、在依赖图上尚未成立，是典型的两代代码并存。

### 命名与概念

命名总体可读，但同一概念存在多组近义词：

- API 用 `tenant_id`，Claim 和 Policy 用 `namespace_key`，召回参数又叫 `namespace`。代码明确承认它们只是
  软标签（`application/ingest.py:341-343`、`api/schemas.py:16,36`），但命名仍暗示了并不存在的隔离能力。
- 时间字段同时有 `occurred_at/observed_at/valid_from/valid_to/recorded_from/recorded_to/created_at`。
  Claim 的双时间字段有清晰语义，但 Event、Job、Conflict 和 Experience 各自选择另一套时间词，缺少一份统一
  的跨模型时间词汇表。
- `valid_until` 只出现在 `StoredClaim`，数据库和运行代码使用 `valid_to`；`ExperienceService` 实际是
  `ExperienceRepository`；`DerivedMemoryMaintainer` 声称维护 mental model/session summary，实际只构建
  observation。名字比实现走得更远。
- `scope` 表示 temporal/permanent，`volatility` 表示 ephemeral/stable，`expires_at` 又表达 TTL。三者在概念
  上可以并存，但 prompt、规则和衰减代码分别解释它们，缺少一个领域对象把三者的组合合法性固定下来。

### 错误处理与数据形态

错误族已经存在，配置、NotFound、Conflict 的 HTTP 映射也较清楚，这是进步；但全局仍不一致：

- 同类“不存在”有时抛 `NotFoundError`，有时抛 `ValueError`，API 再根据字符串或捕获类型转换
  （`api/server.py:202-205,223-228,292-299`）。
- 外部调用有时抛领域异常，有时转成 `RuntimeError`，reranker 则吞掉错误并返回空列表
  （`ingest/embedder.py:70-81`、`recall/reranker.py:34-66`）。降级是合理的，但调用者必须读取可变的
  `last_outcome` 才能区分“没有结果”和“服务失败”，契约不够显式。
- `application/recall.py:189-199` 在审计失败时 `except Exception: pass`；这符合 best-effort 目标，却与项目
  “不要吞异常”的通用约定冲突，且第二次审计失败完全不可见。
- Pydantic 只守 API 和 LLM 边界；应用层、领域层、仓储层仍以可变 `dict[str, Any]` 贯穿。少量 dataclass
  (`StoredClaim`、`ClaimDraft`、`RecallResult`) 又没有进入生产主链。结果不是“灵活”，而是类型系统没有选定：
  新旧字段通过 `value/value_json`、`qualifiers/qualifiers_json` 和 `getattr/get` 防御式拼接。

### 事务边界

Ingest 的核心写入已经正确地把 Claim、冲突、supersede 与 evidence 放进一个 `BEGIN IMMEDIATE`，这是系统中
最成熟的边界。其余区域则同时存在：

- repository 默认自动 commit；
- application 显式开事务并传 `commit=False`；
- domain function 自己 commit；
- worker handler 直接写 SQL；
- `supersede_with_inline()` 根据 `commit` 和 `connection.in_transaction` 推断自己是否拥有事务。

这种混合模式目前能工作，但审查者无法只看层次就知道谁拥有事务。精致的实现应只有一种默认：仓储不提交，应用服务/
worker 用例拥有事务；需要独立原子操作时再明确提供 transaction helper。

## 核心功能实现质量

### 2a. Ingest Pipeline

- 评分：**7.5/10**
- 做得好的地方：
  - `ingest/chunking.py:146-234` 按 conversation、JSONL、普通文本保留结构边界；对话重叠只作为
    `context_only`，截断时递归二分且有深度上限。相比按字符硬切，这是经过思考的实现。
  - LLM 输出先过严格 Pydantic schema，再经过 predicate、canonical attribute、scope 的确定性协调。规则有
    allowlist、alias、高置信模式与审计原因码，不是把模型输出直接入库。
  - `application/ingest.py:191-328` 的 fact hash → 冲突槽 → 语义去重 → 写入/替代/证据链在单事务内执行；
    证据链接使用唯一索引和 `INSERT OR IGNORE`，重复事件也会追加来源证据，可靠性较高。
  - fact hash v2 用 JSON 数组保留字段边界，不可变迁移快照避免未来规则变化破坏历史回填，这体现了良好的
    event-sourcing 意识。
- 需要改进的地方：
  - 属性体系主要是大型手工关键词表（`domain/claims/attributes.py:10-201`），其中包含 Codex、v2rayN、
    DashScope、HL-Mem 等当前项目词汇。它对本机数据有效，却不是稳定的领域语言；新增工具或跨语言表达会静默落到
    宽泛槽。应把“受控 ontology”与“部署特定 alias/hint”分开，并记录 unknown/fallback 的质量指标。
  - `ConflictResolver` 只检查 `existing[0]`（`application/ingest.py:217-250`）。一个槽若已有多个
    candidate/disputed Claim，新 Claim 只与排序第一条决策，可能漏掉另一个矛盾或重复项。决策链应明确是
    “选 current winner”还是“对全部活跃 rival 求闭包”，目前语义不完整。
  - 语义去重只在“没有互斥槽候选”时运行。这个顺序可避免把冲突误删，但也把冲突检测和相似归并拆成互斥分支；
    对同槽 compatible/uncertain 的候选没有继续做重复判断，决策表应显式覆盖所有组合，而不是依赖嵌套分支。
  - `TokenBudget.can_spend()` 与 `record_usage()` 分离，多个 worker 可同时通过预算检查并超支；预算数据库虽然用了
    `BEGIN IMMEDIATE` 记账，却没有原子 reservation。它实现了安全累加，没有实现安全限额。
  - prompt、schema、`_claim()` 又分别做一轮默认值和范围修正；`_parse_legacy_defaults()` 甚至能把旧响应静默
    补成新响应。严格 schema 与宽容兼容同时存在，使“模型违反契约”有时被重试、有时被升级，设计立场不够统一。

### 2b. Recall Pipeline

- 评分：**6.5/10**
- 做得好的地方：
  - FTS 与 dense 各自有序，RRF 使用 `1/(60+rank)`，随后把融合分数归一为 semantic feature；实现本身正确，
    排序还有稳定的时间和 ID tie-breaker。
  - `claim_is_visible()` 是双时间的单一事实来源，FTS/vector 先用 SQL 缩小 valid-time 范围，再用同一纯函数检查
    valid/recorded/status/expires，避免两条检索通道各写一套可见性规则。
  - reranker 失败后回退到先验排序，非法 index 被拒绝，trace 不记录明文 query/value；可观测性和隐私取舍合理。
  - 召回结果会批量装配 evidence、replacement、conflict rivals 和 observation，避免明显的逐 Claim N+1。
- 需要改进的地方：
  - `recall_pipeline.py:155-491` 实际完成候选检索、可见性、RRF、多因子、关系扩展、rerank、trace 和 audit；
    随后的四个阶段函数 `:494-511` 全是原样返回。“Batch 3 拆分”只制造了管线外观，没有形成可测试、可替换边界。
  - 对固定内部仓储仍保留 `except TypeError` 与 `hasattr` 兼容（`:191-230,249-251,312-315`）。
    它会把函数内部真实的 TypeError 误判为旧签名，降低故障可诊断性。
  - Reranker 若返回合法但不完整的结果，非偏好候选会被直接丢弃，而不是把未返回项接在后面。当前 provider 通常返回
    `top_n` 个，但部分响应不应等价于过滤决策。
  - helpful rate 被纳入稳定排序先验，但第一轮数据显示 1,315 条记录仅 2 条是显式反馈。默认 0.5 与极少量样本混合，
    会让“有一次偶然反馈”的 Claim 获得不成比例的排序影响，功能名和数据语义不匹配。
  - packed context 只是按 claim → observation → policy 的固定优先级贪心装箱
    （`application/recall.py:129-158`），没有项目说明所称的跨类型配额，也会让大批 Claim 挤掉 Procedure。
    字符数除二不是可靠 token 估算。它是可用的首版，不是精良的上下文编排器。
  - `matching_policies()` 的中英文 `\w+`/substring 重叠过宽；任一短 token 命中即可注入策略，没有语义分数、
    boundary 或可靠度阈值，3 条策略时问题不大，但不具备自然扩展性。

### 2c. Worker 调度

- 评分：**6.5/10**
- 做得好的地方：
  - `JobRepository.lease_job()` 使用 `BEGIN IMMEDIATE`、过期租约回收、随机 lease token；完成与失败更新都要求
    `id + lease_token + running`，CAS 能阻止旧 worker 覆盖新 worker 的终态。
  - handler 注册表确实替代了 if/elif，未知类型有明确错误，日任务用 idempotency key 保证单日只入队一次。
  - 失败任务按 attempts 回到 pending 或进入 dead，worker crash 后 running job 可在租约过期后恢复。
- 需要改进的地方：
  - 没有租约续期。LLM 提取、归并或重分类超过 5 分钟时，另一 worker 可以重新租用；旧 worker 的“完成”CAS 会失败，
    但 handler 已经产生的 Claim、LLM 成本或策略副作用无法撤回。终态 CAS 可靠，不等于任务 exactly-once。
  - maintenance 是 worker 主循环中的串行硬编码步骤（`workers/worker.py:142-178`）。任一步异常都会退出
    `run_forever`，后续维护和正常 job 都停；不同任务也没有独立的上次成功时间、重试或错误隔离。
  - TTL、decay、purge 同时是 maintenance 直接调用和 JOB_HANDLERS 项；`retry_failed` 查询一个正常状态机永远
    不产生的 `failed` 状态。注册表形式是对的，表内语义却没有收敛。
  - 三个 daily enqueue 函数复制相同 HH:MM 解析与判断，只改错误文本和 job type。这里的 copy-paste 会让时区、
    夏令时或错过执行窗口的修复需要改三处；它是应抽象而未抽象的重复，而不是“为了少写代码”的过度抽象。
  - maintenance 多次调用 `_now()`，同一轮没有统一时间快照；影响很小，却透露出调度语义没有被建模成一次 run。

### 2d. LLM 集成

- 评分：**7/10**
- 做得好的地方：
  - `LLMRequest/Response/Capabilities/ProviderProtocol` 很薄，provider 只负责 payload、response 和能力识别，
    没有造出庞大的通用 SDK；这是合适的抽象粒度。
  - transport retry 与内容级 schema retry 分离：429/5xx/连接超时由 HTTP 层处理，内容不合格由 extractor 带
    错误路径重试，职责清楚。
  - 截断与 schema 失败有独立异常，错误信息包含 provider、model、chunk length 和受控错误路径，诊断性好。
- 需要改进的地方：
  - 任务要求关注的 `json_schema → json_object → text` 实际没有完整实现。类型系统只有 JSON_SCHEMA 和
    JSON_OBJECT；provider 没有 JSON 能力时直接抛错，也没有“纯文本 + 本地 JSON 解析”的最后降级。
  - DashScope/Zhipu 被静态声明为不支持 strict schema，所以通常不会先尝试 schema；`auto` 配置也在工厂中被
    简化成 JSON_SCHEMA 偏好。配置名表达“运行时探测”，实现却主要依赖静态 capability。
  - `retry_http()` 没有 jitter、`Retry-After` 或最大延迟；Embedding 又维护了另一套近似但不相同的内联 retry，
    且不重试 ConnectError。系统声称统一 HTTP 重试，实际上 LLM 与 embedding 仍是两种策略。
  - `LLMConflictJudge` docstring 说“失败最多重试三次”，自身并没有内容级重试；只有 client 的网络重试。
    JSON 解析或 schema 语义错误会直接让 job 失败。文档承诺大于实现。

### 2e. Hermes 适配层

- 评分：**6/10**
- 做得好的地方：
  - HTTP、prefetch、Episode 映射已拆成三个组件；provider 主要协调 hook，较旧的单文件实现更容易理解。
  - Episode/Trace 同步是真实在用的通道，状态、reward、error signature 的映射简单可解释；失败时不阻塞 Hermes
    主流程符合适配器的降级定位。
  - 熔断使用 monotonic time，closed/open/half-open 状态转换的基本路径正确。
- 需要改进的地方：
  - 职责只完成了文件拆分，没有完成契约收敛。`provider.py:88-169` 同时支持“旧异步 prefetch/sync_messages”
    和“新同步 Hermes hook”，用返回值类型和输入类型动态分派；provider 还保留对子组件私有字段的转发属性。
  - 熔断器不是线程安全的。half-open 时多个线程/协程都能通过 `can_call()`，没有“只允许一个探测”的原子门；
    任一并发成功还会关闭其他失败刚打开的电路。对同时存在 prefetch 线程和 async hook 的实现，这不是纯理论问题。
  - `PrefetchCache` 全局只允许一条线程；线程运行时其他 session 的请求直接丢弃
    （`prefetch.py:41-45`）。缓存没有 TTL、query/version key 或会话结束失效，旧文本会无限期按 session_id 返回。
    这既可能陈旧，也可能在 session ID 复用时泄漏上下文。
  - `_sync_messages()` 吞掉 Episode 同步异常，`_sync_episode_sync()` 吞掉 RuntimeError/HTTPError，
    `_sync_post()` 只返回 bool。降级符合产品目标，但没有计数、日志或 health 暴露，真实数据链一旦中断很难定位。
  - `on_session_end()` 是空 hook，恰好错过了清理 prefetch、提交最终 task outcome 和关闭 Episode 的自然位置。

## 数据模型设计评估

数据模型评分：**6.5/10**。

### 做得好的地方

- Event 与 Claim 分离，Claim 通过 `evidence_links` 指回不可变事实来源；supersedes 既有结构字段也有证据关系，
  可以解释“为什么这条记忆存在/失效”。
- Claim 同时保存 valid time 与 recorded time，半开区间规则一致；`known_as_of` 可以回答“当时系统知道什么”，
  这比只有 `created_at` 的普通 RAG memory 成熟。
- FTS5 external-content 表与触发器、WAL、外键开启、常用状态/冲突/证据索引都符合本地优先 SQLite 的定位。
- Experience 的 episode/trace/policy 关系有约束，trace sequence 唯一，policy trigger 在 namespace 内唯一；
  lease token migration 补上了并发终态 CAS 所需字段。

### 主要问题

- Claim 主表的核心约束偏弱。初始 schema 对 predicate/value/valid_from 允许 NULL，status 只有注释没有 CHECK；
  `012_status_check.sql` 只 `SELECT 1`，并没有兑现文件名。`update_status()` 只验证目标枚举，不验证转换矩阵，
  多处 SQL 又直接改状态。状态机在 Python 中存在，但不是所有写路径的事实来源。
- Evidence link 没有对 event/claim/episode 的多态外键，也没有 relation/weight CHECK。SQLite 无法直接表达多态 FK
  可以理解，但当前至少缺少应用级验证或 orphan 检查；“完整证据链”依赖每个调用者自律。
- `conflict_cases.status/decision`、`retrieval_feedback.memory_type/helpful`、`derivations.kind/status` 等仍是自由 TEXT。
  系统已经有多套状态机，却只对 Episode/Policy 建了数据库约束，品味不一致。
- 双时间在 Claim 的 FTS/vector 主路径应用正确，但它并不是“每个查询”的统一要求：
  dedup/conflict/consolidation 主要按 status/recorded watermark 查询；Observation 只看 active；policy/experience 只用
  单时间。若这些是刻意的“当前写入决策”语义，应在 repository 方法名中表达，而不是让调用者推断。
- `supersede_with_inline()` 会把旧 Claim 的 `value_json` 改写成包含 old/new 的 envelope。这保留了展示便利，却破坏
  “Claim value 是原子事实值”的稳定语义，也会改变 FTS 内容。更干净的模型是旧值保持不变，替代关系和新值由新
  Claim 表达。
- `tenant_id/namespace_key` 目前不是隔离边界：显式记忆、maintenance、retention、policy induction 多处写死
  `default`，API recall audit 也固定 default。保留软标签可以，但它不应在接口上制造多租户已经成立的印象。
- 第一轮数据中的 `memory_relations=0`、`audit_review=0` 与几乎无显式 feedback，说明 schema 含有三块超前于
  产品闭环的管理面。表本身不伤害运行，但它们增加了迁移和认知成本，却没有形成可验证价值。

## 缺失功能分析

以下不是“再加几个表”，而是一个精致记忆系统已经需要的闭环：

1. **离线召回质量评估与回归门槛。** 当前有 search trace 和曝光记录，却没有 gold query set、Recall@K、
   Precision@K、MRR、冲突召回率、时间切片正确率或版本对比。没有它，多因子权重、RRF、reranker 和属性规则的
   调整只能凭感觉；这也是当前最不该缺的能力。
2. **真实结果反馈闭环。** Hermes 已经知道 Episode 最终状态和 reward，却没有把它关联回该任务实际使用的
   query/Claim；`used_by_model` 也几乎从未填写。应该采集“哪些记忆被注入、哪些被模型引用、任务是否成功”的
   最小闭环，而不是继续把曝光称为 feedback。
3. **可恢复、可观测的 worker 运维面。** 需要 lease heartbeat、maintenance 子任务隔离、dead-letter 查看/
   重放、上次成功时间和失败计数。当前能从 crash 恢复租约，但不能安全处理长任务，也无法知道某项维护是否长期停摆。
4. **备份/恢复的交付入口与演练。** 本地优先系统的数据只存在本机，`storage/backup.py` 算法已经有了，却没有
   CLI/计划任务/恢复校验流程。相比 PostgreSQL 探针和关系扩展，这应有更高产品优先级。
5. **派生记忆的真正总结与重建策略。** 当前 lifecycle 有 TTL、衰减、归档、supersede、retract 和 observation，
   但 observation 只是同槽值拼接；`mental_model/session_summary` 没有 builder。需要基于证据水位的增量总结、
   stale 队列、重建预算和可解释的压缩策略，而不是先声明通用 kind。
6. **数据完整性巡检。** 应定期检查 orphan evidence、无 replacement 的 superseded Claim、长时间 running
   Episode、无效时间区间、embedding dim/model 混杂和 FTS 一致性。事件溯源系统的价值取决于历史可解释性，这类
   巡检比再增加检索通道更重要。
7. **隐私/敏感度的实际执行。** EventInput 和审计记录了 `sensitivity`，但召回、保留、导出和日志没有按敏感级别
   做策略。字段存在却不影响行为，会给使用者错误安全感。

不该继续扩张的部分也很明确：在上述闭环完成前，不应继续发展 PostgreSQL、独立关系图、更多 derivation kind 或
新的只读管理端点。它们不是方向错误，而是优先级错误。

## 代码品味问题

1. **假阶段制造架构观感。** `recall/recall_pipeline.py:113-152,494-511` 把全部工作塞进
   `_collect_candidates()`，再经过四个 no-op 阶段。好的分层会分配数据契约与责任；这里只增加了名字。
2. **运行时 monkeypatch 私有方法。** `api/server.py:125-127,261-263` 用 lambda 覆盖服务私有方法，
   且行为重复。读者会误以为 API 有特殊队列策略，实际没有。
3. **import 放在文件末尾。** `mcp/server.py:114` 在所有类定义之后才导入 `components`。Python 虽能运行，
   但违背常规阅读顺序，也像为修循环依赖/测试临时补上的拼接。
4. **过期承诺未兑现。** 多个兼容模块明确写“will be removed in v0.6.0”，而包版本已经是 0.6.0；
   `LLMExtractor.from_env()`、Hermes 静态别名和 repository re-export 也仍在。版本承诺失信会让弃用策略失去意义。
5. **同一工具重复实现。** `core/vector.py:9-18` 与 `ingest/embedder.py:13-21` 都编解码 float32；
   `extended_pipeline.py:8-39` 与正式召回/上下文装配各有另一套 RRF、budget pack；HTTP retry 也有两套。
6. **防御式兼容掩盖真实错误。** `recall_pipeline.py:191-230` 捕获 TypeError 猜测旧签名；这是“过度聪明”
   的典型例子，短期兼容方便，长期让真实类型错误转入另一条错误路径。
7. **状态机口号大于强制力。** `storage/claims.py:92-100` 只验证目标状态，`cli.py:68-77`、worker 和
   consolidator 多处直接 SQL 改状态。既然已经建立 `assert_transition()`，绕过它的路径就更令人意外。
8. **配置集中化没有完全完成。** `workers/decay.py:10-26` 在 import/调用时直接读环境变量，`Database` 也自行读
   pool size，`config.py` 还保留未使用 worker 常量；它们与“Settings 启动时解析一次”的设计语言冲突。
9. **docstring 多数只复述函数名。** 例如“返回指定派生记忆”“执行同步 POST 请求”“更新派生记忆状态”没有解释
   边界、幂等性或为何允许降级。相反，`AuditLogger`、chunking 和不可变 migration snapshot 的说明较好。
10. **风格不统一。** 中英文 docstring/注释混用，部分文件遵循 black，`ranking.py`、`observation.py`、
    `decay.py` 有明显手工压行；`claims.py:19-33` 在 dataclass 后才继续 import。单点都不严重，合起来削弱成品感。
11. **静默失败过多。** Audit 的 best-effort 有明确理由，但 Hermes 多处 `except Exception` 后只返回空值或
    `pass`，而没有可消费的 telemetry。降级本身有品味，无法区分降级原因则没有。
12. **接口展示了尚不存在的能力。** MCP 类名叫 Server 却没有 transport/入口，Postgres 类名叫 Database 却只有
    connect probe，namespace 看似隔离但只是标签。精致系统会让名字精确匹配交付程度。

## 最值得改进的 5 个品质问题（优先级排序）

1. **建立召回质量评测闭环，再调整算法。** 建立版本化 gold set 与 Recall@K/MRR/时间正确率/冲突正确率，
   把 search trace 变成可比较指标；否则当前最核心的“记得准不准”没有工程事实。
2. **统一事务、状态机与错误契约。** repository 默认不 commit，application/worker 拥有事务；所有 Claim/
   Episode/Conflict 转换经过同一 guard；NotFound/Validation/ExternalService 使用统一异常族，消除字符串判断。
3. **收敛召回与写入的两代代码。** 删除 no-op 阶段和旧签名兼容，让真正阶段各自拥有输入输出；统一 dict/dataclass
   选择、具体仓储 import、向量/重试实现。目标不是少代码，而是让每层只存在一种正确做法。
4. **让 worker 在长任务和局部故障下真正可靠。** 增加 lease heartbeat；把 maintenance 拆成可独立记录、失败
   隔离和重试的任务；删除永远不会入队的 handler，并提供 dead job 查看/重放。
5. **完成 Hermes/反馈/缓存闭环。** 明确只保留一代 hook 契约；熔断 half-open 单探测并加并发保护；prefetch 按
   session+query 带 TTL/失效；在 session end 提交实际使用与 task outcome。这样 Experience 和 helpful ranking
   才从“有数据”升级为“数据可信”。

## 审查范围说明

本轮读完了 `src/hl_mem/` 下全部 Python 源码、SQL migration、v006 不可变快照及 Hermes 插件清单；品质判断直接
采用第一轮报告给出的数据库行数和实际调用方事实，没有重复采集运行数据。按任务约束，未修改 `src/`，未运行 pytest。

## Hermes 评审回应（第三轮收敛）

### 对 10 条反馈的逐条回应

1. **AGREE：假阶段函数。** Hermes 的验证与报告结论一致。当前
   `_filter_and_score()`、`_expand_related()`、`_rerank()`、`_finalize()` 没有独立契约，也没有承担任何工作；
   它们不是可替换阶段，只是对 `_collect_candidates()` 已完成工作的重复命名。具体修复建议见下文“假阶段函数的
   修复建议”。

2. **AGREE：运行时 monkeypatch 私有方法。** 这不是测试注入，而是两个生产 REST 路由在构造
   `IngestService` 后，用 lambda 把服务的 `_queue_event` 替换为 `api.server._queue_event()`。两者当前行为近似，
   推测动机是迁移期间复用旧 API helper，或让路由持有的 connection 继续参与同一事务；但这种动机没有形成显式
   契约。正确做法是由 `IngestService` 唯一拥有入队行为及事务参数，路由只调用公开用例。若测试需要观察或替换
   入队，应向服务构造器注入一个有类型的 `EventQueue`/callback fake，或 mock 公开依赖，而不是在生产路由中改写
   私有方法。

3. **AGREE：过期兼容层未清理。** “will be removed in v0.6.0”与当前版本直接矛盾，必须修正文档化的删除版本或
   实际删除。这里需要区分“版本承诺失信”和“运行时存在两套实现”：纯 re-export 本身未必产生行为分叉，但失期的
   弃用计划仍应在明确的兼容窗口内兑现。

4. **AGREE：同一功能重复实现。** `core.vector` 与 `ingest.embedder` 的向量编解码、正式召回与
   `extended_pipeline` 的 RRF/预算装箱、`http_utils.retry_http()` 与 embedding 内联 retry 都会让修复、参数和
   错误语义发生漂移。这些不是单纯多入口，而是独立实现，属于必须收敛项。

5. **AGREE：事务边界不一致。** repository 自动提交、application 传 `commit=False`、domain 自行提交以及
   worker 直接写 SQL，使事务所有权无法由层次推断。目标契约应是：repository 只执行数据操作，application/worker
   用例拥有事务；独立原子操作通过明确命名的 transaction helper 提供，不再由 `commit` 布尔值和
   `connection.in_transaction` 猜测所有权。

6. **AGREE：熔断器非线程安全。** prefetch 线程和同步/异步 hook 会共享同一状态，half-open 缺少原子单探测门，
   因而可能并发放行并发生成功/失败互相覆盖。需要用锁保护状态转换，并为 half-open probe 设置唯一占用标记；
   只有占用探测权的调用可以关闭或重新打开电路。

7. **ADJUST：接受优先级调整，并进一步拆开“收敛”与“契约”。** 我同意先在稳定实现上建立评测基线。原排序把
   “可量化核心价值”误当成了“实施前置依赖”。修订后先消除活跃的双实现和假边界，再统一事务/状态/错误契约，
   然后建立召回评测闭环。需要补充一点：不是等所有 polish 完成后才评测；收敛主链后应立即固定最小 gold set，
   避免后续收敛继续无回归门槛。

8. **AGREE：接受“两代代码”精确界定要求。** 原报告把活跃双实现、旧签名兼容和纯 re-export 混在同一个措辞里，
   不够精确。下文按“是否有两条活跃行为路径、是否可能产生不同结果”重新分类。

9. **AGREE：docstring 与风格降级为次要 polish。** 事实判断不撤回，但同意它们不应与事务、并发、假阶段等结构性
   风险处于同一层级。后续审查应将原品味问题 #9、#10 移到“次要 polish 清单”，只在主链收敛后批量处理，且不占
   top-5。

10. **AGREE：缺失功能需要实施时序。** 七项并非同优先级。前三项应在主链代码收敛后进入最近里程碑，其余四项按
    发布风险和依赖关系分期，具体排序见下文“缺失功能 top-3”。

### 修订后的“最值得改进的 5 个品质问题”

1. **先收敛活跃的两代实现与假边界。** 删除召回 no-op 阶段或赋予其真实契约；统一 RRF、预算装箱、向量编解码和
   HTTP retry；移除旧签名的 `TypeError` 猜测、生产 monkeypatch 以及 Hermes 动态双契约。完成标准是每项核心
   行为只有一个事实来源，而不是仅减少文件数量。
2. **统一事务、状态机与错误契约。** repository 默认不提交，application/worker 拥有事务；Claim、Episode、
   Conflict 的状态变更统一经过 guard；NotFound、Validation、ExternalService 使用明确异常族。此项紧随代码
   收敛，因为它决定写入正确性，也决定后续 worker 和反馈功能能否安全落地。
3. **建立召回质量评测与回归门槛。** 在收敛后的唯一召回实现上固定版本化 gold set，至少覆盖 Recall@K、MRR、
   时间切片正确率和冲突召回正确率，并记录配置/模型版本。以后修改 RRF、权重、reranker 或属性规则必须与同一基线
   比较。
4. **让 worker 在长任务与局部故障下可靠。** 增加 lease heartbeat；把 maintenance 子任务变成可单独记录、
   失败隔离和重试的工作单元；提供 dead job 查看/重放，并删除状态机永远不会产生的 handler 路径。
5. **收敛 Hermes 并完成真实反馈闭环。** 只保留一代 hook 契约；为熔断 half-open 加单探测并发保护；prefetch
   使用 session+query/version key、TTL 和 session-end 失效；把实际注入/引用的 Claim 与 Episode outcome/reward
   关联。这样 helpful ranking 才有可信数据来源。

排序原则是“先修复会让后续工作建立在不稳定基础上的问题”。因此评测从原 #1 调整到 #3；它仍应在结构收敛后立即
开始，而不等待 worker、Hermes 和全部 polish 完成。

### “两代代码”的精确清单

#### 必须现在收敛

以下项目存在两条活跃行为路径，或兼容逻辑会改变运行结果/掩盖真实错误：

1. **召回管线的真实实现与假阶段。** `_collect_candidates()` 已完成检索、可见性、RRF、评分、关系扩展、
   rerank、trace 和 audit，四个命名阶段同时处于调用链但为 no-op。应选择真实分阶段或单函数，不应继续维持两套
   架构叙述。
2. **两套 RRF 与上下文预算装箱。** `recall/recall_pipeline.py` 的正式融合、`recall/extended_pipeline.py`
   的 `reciprocal_rank_fusion()`，以及 `application/recall.py` 与 `extended_pipeline.budget_pack()` 的装箱
   逻辑具有不同入口和语义；应各自确定唯一生产实现，实验实现移到测试/实验命名空间或删除。
3. **两套向量 BLOB 编解码。** `core/vector.py` 的 `encode_vector()/decode_vector()` 与
   `ingest/embedder.py` 的 `pack_vector()/unpack_vector()` 都在表达 float32 存储协议，返回类型和校验又不同。
   存储格式必须只有一个事实来源。
4. **两套 HTTP retry。** LLM 使用 `http_utils.retry_http()`，embedding 在 `_request()` 内自行退避；重试异常
   集合、连接错误处理、延迟和未来的 `Retry-After` 支持会分叉。应统一策略，并允许各 provider 只配置参数。
5. **两种召回仓储签名。** `recall_pipeline.py` 用 `except TypeError` 回退旧参数，并以 `hasattr` 选择旧检索路径。
   新旧调用均可在生产执行，而且真实 TypeError 会被误判，必须迁移调用方后删除运行时猜测。
6. **Hermes 同步/异步两代 hook 契约。** `prefetch()` 依据 `session_id` 改变返回类型，`sync_turn()` 依据
   `content` 类型在旧异步消息同步和新同步 hook 间分派；这是同一公开方法的双重运行语义，必须选择当前 Hermes
   清单要求的一代契约，旧契约若仍需过渡应使用不同方法名和明确 adapter。
7. **四种事务所有权。** repository 自动提交、application 的 `commit=False`、`domain/relations.py` 自提交、
   worker 直接 SQL 都是活跃写路径。它们可能造成局部提交和状态 guard 绕过，必须按应用用例拥有事务的规则收敛。
8. **API 入队双实现。** `IngestService._queue_event()` 与 `api.server._queue_event()` 同时存在，REST 路由通过
   monkeypatch 选择后者。当前代码近似不代表契约一致；应保留服务层唯一实现并删除生产期替换。
9. **内部数据形态的双轨兼容。** 生产主链同时接受 `value/value_json`、`qualifiers/qualifiers_json` 和
   dict/dataclass 风格访问。应先选定稳定内部类型，在 API/数据库边界一次转换；否则字段演进会继续依赖
   `getattr/get` 猜测。

#### 可按计划清理

以下项目的旧入口主要是 re-export/alias，并带弃用告警；只要确认没有隐藏实现和外部兼容窗口，它们不会自行产生
不同业务结果：

1. `storage/repository.py` 对具体 `storage.claims/events/evidence/experience/jobs` 仓储的 re-export。
   项目内部仍有大量旧入口导入，应先机械迁到具体模块，再在下一个明确的破坏性版本删除兼容入口。
2. `api/pipeline.py`、`ingest/embeddings.py`、`recall/router.py`、`recall/policy.py`、
   `recall/dedup.py`、`recall/conflict.py`、`recall/attribute_map.py` 等带 `DeprecationWarning` 的兼容
   re-export。它们应统一修正已经失期的“v0.6.0 删除”声明，并设置实际截止版本。
3. `LLMExtractor.from_env()`。它是旧构造便利入口，已经提示改为显式注入 `LLMClient`；若生产主链不依赖它，可按
   兼容窗口删除，不应与 HTTP 双 retry 视为同等级风险。
4. `HermesMemoryProvider = HLMemProvider`、插件层 `HlMemProvider` 等静态类名别名。别名本身指向同一实现，
   可配合 Hermes 插件清单和外部调用方版本计划删除；真正必须先修的是同一实例内部的同步/异步双契约。

“可按计划清理”不等于无限期保留。最低要求是：项目内新代码停止使用旧入口、弃用截止版本真实可执行、到期有删除
检查；否则正常演进痕迹会重新变成维护债务。

### 假阶段函数的具体修复建议

建议采用**真实分阶段**，而不是把全部逻辑永久留在 `_collect_candidates()`。理由不是追求流水线外观，而是当前
召回确实已经存在五种可独立验证的责任：通道检索、可见性与融合评分、关系扩展、rerank、trace/audit 收尾；同时
reranker、关系扩展和时间可见性都有独立失败与降级语义，值得有稳定边界。

建议的最小阶段契约如下：

1. `_collect_candidates(request) -> CandidateSet`：只执行 FTS/dense 检索，返回两个有序通道、统一时间快照、
   intent、candidate limit 和各通道耗时；不做 RRF、关系扩展、rerank 或 audit。
2. `_filter_and_score(candidate_set) -> ScoredCandidates`：应用 `claim_is_visible()`、去重、helpful rate、RRF 与
   多因子先验，返回稳定排序及按 claim ID 索引的 feature/pre-score；这里是排序前唯一事实来源。
3. `_expand_related(scored, config) -> ScoredCandidates`：可选地加入关系候选，并对新增集合重新计算受
   `max_access` 影响的 feature；禁用时可以显式返回原值，但阶段本身仍拥有明确的可选功能，不是伪装工作已完成。
4. `_rerank(expanded, reranker) -> RankedCandidates`：只处理 provider 调用、结果校验、部分结果补尾和 fallback，
   显式返回 outcome、rerank score 与耗时，不读取可变 `last_outcome` 之外的隐式状态。
5. `_finalize(ranked, trace_context) -> list[Claim]`：执行 limit/preference 保留、trace/audit 和最终 `_score`
   装配；audit 失败策略在这里显式定义。

这些中间结果应使用小型 dataclass，而不是继续传入可变 `list[dict[str, Any]]`。第一步重构必须保持排序结果不变：
先为现有输出建立 characterization tests，再逐段移动逻辑，每次只移动一个阶段。若团队不愿维护这些中间契约，
则次优但诚实的修复是立即删除四个 no-op 和管线式 docstring，把函数改名为单一 `_recall_claims()`；继续保留假阶段
是最差选择。

### 缺失功能 top-3 与其余时序

在上述代码与事务主链收敛之后，最关键的三项是：

1. **离线召回质量评估与回归门槛。** 它回答记忆系统是否“记得准”，是后续排序、reranker、冲突与时间语义改动的
   工程判据；应作为收敛完成后的第一个功能里程碑。
2. **真实结果反馈闭环。** 记录实际注入/引用的 Claim，并与 Episode outcome/reward 关联；它回答哪些记忆真正帮助
   任务，是 helpful ranking、个性化和策略学习可信化的前提。
3. **可恢复、可观测的 worker 运维面。** heartbeat、维护任务隔离、dead-letter 重放和成功/失败指标直接保护
   提取、衰减、归档与反馈异步链；没有它，精致的算法仍可能静默停摆或重复产生副作用。

其余四项安排如下：

- **备份/恢复入口与演练**：紧随 top-3，在首次宣称“可长期保存个人记忆”或稳定版发布前完成；本地优先意味着它是
  发布门槛，不是远期增强。
- **数据完整性巡检**：与 worker 运维面同一阶段设计，在 heartbeat/dead-letter 稳定后接入定时任务；至少先提供
  只读检查和明确告警，再考虑自动修复。
- **隐私/敏感度实际执行**：在允许敏感数据、导出或多用户使用前完成；当前单机受控试用期可排在运维与备份之后，
  但若产品边界提前扩展，此项立即提升为发布阻断项。
- **派生记忆的真正总结与重建策略**：放在评测、反馈和 worker 可靠性之后。它依赖可信证据水位、重建任务与质量
  判据；在这些基础缺失时扩展 `mental_model/session_summary` 只会增加另一组半成品接口。
