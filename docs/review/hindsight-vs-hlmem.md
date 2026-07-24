# Hindsight 与 HL-Mem 架构对比评审

> 评审日期：2026-07-24  
> 评审基线：HL-Mem v0.10.1、solo 单机部署、SQLite、约 514 claims  
> Hindsight 基线：项目提供的 `.pyc` 逆向概要及 `docs/review/hindsight-analysis.txt`

## 结论摘要

Hindsight 和 HL-Mem 的主要差异不是“记忆能力完整与否”，而是产品边界不同：

- Hindsight 已按多租户、服务化、大数据量和多模型运营来设计，优势集中在可扩展检索基础设施、Bank 级配置、LLM 路由和运营可观测性。
- HL-Mem 是本地优先的单机系统，优势集中在双时间、证据链、冲突状态机、TTL、slot/tags、Experience 通道和 SQLite 下的低运维成本。
- 对当前 514 claims 而言，迁移 PostgreSQL、引入 ANN、多 Bank 独立索引或完整 LiteLLM Router 都会明显过度建设。真正值得借鉴的是 Hindsight 的“控制面”：LLM 调用账本、健康状态、进度模型、可试运行提取，以及清晰的配置访问边界。

建议优先顺序为：

1. **现在做：LLM 调用级可观测性。**
2. **现在做：中文全文检索基准和 tokenizer 可替换边界。**
3. **现在做：长任务持久化进度。**
4. **计划做：Dry-run extraction 与显式 consolidation scope。**
5. **计划做：reranker/provider 工厂化，但暂不引入多个重型实现。**
6. **达到规模阈值后做：向量后端抽象与 ANN。**
7. **出现真实租户需求后做：Bank/MemorySpace 级强隔离和分层配置。**

一个重要的事实校正：当前源码中的 LLM 默认模型是 `qwen3.7-plus`，且已经支持 `dashscope`、`zhipu`、`openai_compatible` 三种 provider；它不是代码层面只支持 glm-5.2。真正缺少的是同一操作的多模型路由、fallback、健康探测和细粒度 token/latency 记录（`src/hl_mem/settings.py`、`src/hl_mem/components.py`、`src/hl_mem/llm/`）。

## 评估口径

差距等级表示架构能力差异，不等于当前业务风险：

- **无差距**：能力等价或 HL-Mem 已有更适合自身目标的实现。
- **小差距**：缺少可插拔性或操作体验，但不影响当前核心正确性。
- **中等差距**：在质量、诊断或后续演进上已有明显成本。
- **大差距**：面向相同部署目标时缺少关键基础能力。

行动标签：

- **应该做**：当前规模也能产生直接收益，且侵入较低。
- **计划做**：先保留边界或纳入路线图，等评测/需求触发。
- **暂缓**：当前收益不足以覆盖复杂度和运维成本。

---

## 1. 向量检索

**差距评估：大差距（架构能力），当前影响：小。**  
**建议：计划做后端边界，ANN 暂缓。**

### 现状差异

HL-Mem 将 2048 维 float32 embedding 存为 SQLite BLOB。召回时先查询 namespace 和双时间可见范围内的全部向量，再在 Python 中逐条解包、计算余弦并全排序。源码已明确注释：100k × 2048 的全扫描约需处理 819 MB，并应在接近该规模前重新考虑索引检索（`src/hl_mem/storage/claims.py:226`、`:248`，`src/hl_mem/core/vector.py`）。

Hindsight 提供 pgvector HNSW、diskann、vectorscale、alloydb_scann，并按后端设置 ANN 查询参数；小数据量时延迟建索引，避免“为了索引而索引”。其成熟点不只是 ANN，而是把“全扫还是索引、使用哪种索引、如何调参”做成了可选择的存储策略。

### 当前场景的实际影响

514 claims 的原始向量约为 `514 × 2048 × 4 ≈ 4.2 MB`。即便算上 Python 解包和排序开销，全扫通常仍不是主要瓶颈，远程 embedding 和 reranker 延迟更可能占主导。此时迁移 PostgreSQL 会引入服务管理、备份、迁移、连接池和一致性边界，收益很低。

当前实现仍有三个演进风险：

1. 每次相似度计算都从 BLOB 解包为 Python 数值，常数开销较大。
2. 先取全部可见向量再排序，复杂度随 namespace 内活跃 claims 线性增长。
3. 存储层接口直接暴露 BLOB 和 Python cosine，未来切换 ANN 会侵入 repository 与 staged pipeline。

### 最小可行方案

先不引入 PostgreSQL，增加一个窄的 `VectorSearchBackend` 协议，输入 query vector、namespace、双时间过滤和 limit，输出 `(claim_id, score)`；SQLite 全扫作为默认实现。增加可观测阈值：

- 记录 `embedded_candidate_count`、dense 阶段耗时和 P95；
- 以实测触发迁移，而非仅按行数：例如 namespace 内活跃向量达到 10k，或 dense P95 连续超过 50 ms；
- 达到阈值后再评估 sqlite-vec/FAISS 本地后端；只有出现多进程共享、百万级数据或服务化要求时再选择 pgvector/HNSW。

Hindsight 的“自动延迟建索引”值得保留为未来策略，但当前最合理的结果恰好是继续全扫。

---

## 2. 全文检索

**差距评估：中等差距，当前影响：中等。**  
**建议：应该做评测和可替换边界；更换数据库暂缓。**

### 现状差异

HL-Mem 不是简单字符串匹配：`claims_fts` 和 `claims_tags_fts` 均为 FTS5 虚表，查询使用 SQLite `bm25()` 排序，并通过触发器保持索引同步（`src/hl_mem/storage/migrations/001_initial.sql`、`018_claims_tags_fts.sql`、`src/hl_mem/storage/claims.py:446`）。查询还进行了语法转义并受 namespace、状态和双时间过滤约束。

但建表没有显式 `tokenize=`，因此使用 FTS5 默认 tokenizer。对英文和以空格分词的技术文本基本够用；对中文词边界、同义词、混合中英文、部分词匹配和召回稳定性不如 ParadeDB `pg_search` + `lindera(chinese)`。Hindsight 的优势是 tokenizer 可配置且 CJK 是一等场景。

### 当前场景的实际影响

这比向量 ANN 更可能影响当前质量。HL-Mem 的主要语料和 slot 规则明显包含大量中文；514 claims 虽小，但分词质量与数据规模无关。Dense 通道和 tag boost 能补回部分结果，却不能完全替代精确实体、配置项、版本名和中文关键词检索。

差距不能仅按“FTS5 对 ParadeDB”判断。HL-Mem 已有 FTS + dense + 可选 tag channel 的 RRF 融合，以及 reranker，系统级差距小于单看全文引擎的差距。是否值得换引擎必须由现有中文 eval 数据回答。

### 最小可行方案

1. 在 `tests/eval` 增加中文词边界、混合中英文、别名、短查询和 exact identifier 用例，分别报告 FTS-only 与混合召回指标。
2. 在 repository 内形成 `TextSearchBackend` 边界，不改上层 RRF 契约。
3. 先尝试低侵入策略：写入时增加规范化检索文本或受控的中文词/别名展开；确保 A/B 每次只改变 tokenizer/展开策略一个变量。
4. 只有 FTS-only 和端到端指标证明存在持续缺口，且 SQLite 可用方案不能满足时，再考虑 ParadeDB/PostgreSQL。

不建议现在仅为 lindera 迁移整个存储栈。

---

## 3. Reranker

**差距评估：中等差距，当前影响：小。**  
**建议：计划做 provider 工厂，多个后端实现暂缓。**

### 现状差异

HL-Mem 已定义 `RerankerProtocol`，组件工厂支持 off/fake/real，但真实实现只有 DashScope HTTP API 风格的 `gte-rerank-v2`，失败时返回空结果并由召回流程降级（`src/hl_mem/protocols.py`、`src/hl_mem/components.py`、`src/hl_mem/recall/reranker.py`）。模型名和 base URL 可配置，所以“固定”主要体现在请求协议和实现，而非字符串不可改。

Hindsight 的 CrossEncoder 明确区分 Local SentenceTransformer、Remote TEI、Cohere 等部署形态，能在隐私、成本、GPU 利用和服务稳定性之间切换。

### 当前场景的实际影响

solo 单机只有一条已经可用的远程 reranker 链路时，多后端不会直接提高召回质量，反而会增加依赖、模型下载、GPU 显存竞争和测试矩阵。RTX 5070 Ti 使本地 cross-encoder 在未来具备可行性，但不等于现在就需要常驻一个模型。

真正的问题是当前 `Reranker` 将 DashScope URL、payload 和响应结构封装在一个具体类中，未来增加 TEI 会产生条件分支；另外，当前健康页只表明 reranker 模式，不能验证远端是否可用。

### 最小可行方案

- 保留 `RerankerProtocol`，把现有类命名/定位为 `DashScopeReranker`；
- 增加 `reranker_provider` 配置和 registry，默认仍为 DashScope；
- 先补 provider、model、latency、outcome、candidate count 的观测字段；
- 只有出现离线运行、隐私约束、远程费用或稳定性问题后，再实现 `LocalCrossEncoderReranker` 或 `TEIReranker`，二选一即可。

不要为了“多后端”同时接入 Local、TEI、Cohere。

---

## 4. LLM 管理

**差距评估：中等差距，当前影响：中等。**  
**建议：可观测性应该做；多模型 Router 计划做/暂缓。**

### 现状差异

HL-Mem 已有较好的基础抽象：

- provider-neutral 的 `LLMRequest` / `LLMResponse`；
- DashScope、Zhipu、OpenAI-compatible adapter；
- HTTP retry、timeout；
- JSON Schema 到 JSON Object 的能力降级；
- 响应中保留 `usage_total_tokens` 和 `raw_request_id`。

但运行时一个 `LLMClient` 只绑定一个 provider/model。没有按 operation 路由、fallback model pool、熔断、单调用持久化记录，也没有 input/output/cached token 拆分（`src/hl_mem/llm/`、`src/hl_mem/components.py`）。

Hindsight 的 LiteLLM Router 解决的是多模型运营：不同操作选不同模型、失败切换、统一统计。这个能力比“支持多个 provider 类”高一层。

### 当前场景的实际影响

单用户不需要复杂的负载均衡，但提取、冲突判断、归并、去重等操作共享一个模型时，任何供应商抖动都会影响整条维护链路。当前只有提取器累加 `last_usage_tokens`，无法可靠回答：

- 哪类操作最耗 token；
- 哪个 provider/model 在失败；
- schema retry 和 transport retry 分别耗费多少；
- 某个 claim 的 LLM 决策属于哪条 trace；
- fallback 是否改善了成功率或只是增加成本。

因此，LLM 可观测性是当前就有价值的；完整 Router 不是。

### 最小可行方案

在 `LLMClient.complete()` 外围建立统一调用 span，持久化：

- `trace_id`、`span_id`、`parent_span_id`；
- operation（extract/conflict/consolidate/dedup 等）；
- provider、model、structured mode、attempt；
- status、error_class、raw_request_id；
- input/output/cached/total tokens（provider 不提供时允许为空）；
- latency、started_at。

先支持一个 primary + 一个可选 fallback，且只对明确的可重试故障切换；fallback 配置为空时行为保持不变。等至少两个操作确实需要不同模型或出现稳定性数据后，再考虑 LiteLLM Router。避免在尚无运营需求时引入一个新的核心依赖层。

---

## 5. 配置系统

**差距评估：中等差距，当前影响：小。**  
**建议：静态/动态访问边界计划做，Bank 级覆盖暂缓。**

### 现状差异

HL-Mem 的 `Settings` 是冻结 dataclass，启动时统一从环境变量构建并校验，工厂显式注入组件；配置数量已经很多，且生产模式有组合校验和非敏感 snapshot（`src/hl_mem/settings.py`、`src/hl_mem/components.py`）。这对单进程单用户是简单而可靠的。

Hindsight 将配置区分为：

- static：数据库、扩展等服务器级配置，不允许从 Bank 上下文误覆盖；
- hierarchical：允许 tenant/bank 层覆盖；
- 通过 `ConfigFieldAccessError` 防止在错误层级读取。

差距不在“环境变量数量”，而在配置的作用域和访问纪律。

### 当前场景的实际影响

当前没有真实租户或多个独立 memory bank，所有配置全局一致反而更易理解。为 514 claims 引入数据库配置覆盖、热加载、继承合并和缓存失效是不必要的。

不过源码已经出现作用域不一致：API 接受 `tenant_id`/`namespace`，注释明确它们只是软标签；部分后台维护和策略逻辑仍使用 `default`。如果未来直接增加可覆盖配置，会把软隔离误包装成强隔离。

### 最小可行方案

先在类型和文档层把字段标记为 `static`、`memory_space` 或 `request`，但不实现数据库覆盖。组件工厂只接受解析后的配置视图，并禁止业务代码直接读环境变量。等出现第二个需要不同模型、TTL 或检索策略的真实 memory space 后，再增加：

`global defaults -> memory-space override -> request-safe override`

其中数据库路径、schema、向量后端等保持 static；模型、召回权重、TTL 可列入 memory-space；limit、debug、token budget 可保持 request 级。

---

## 6. 可观测性

**差距评估：中等差距，当前影响：中等。**  
**建议：应该做，优先补 LLM 与任务指标，不重建通用 tracing 平台。**

### 现状差异

HL-Mem 并非缺少观测：

- `AuditLogger` 记录 phase/action/outcome/duration、trace_id 和 event/claim/query/job 维度；
- `SearchTrace` 记录 FTS、dense、tag、relation、fusion、reranker、assembly 各阶段耗时；
- 每个候选保留通道排名、分数、过滤原因、rerank 结果和最终是否入选；
- `/healthz`、`/v1/stats` 和 job counts 提供基础状态。

这些设计对“为什么召回这条记忆”甚至比只做基础 span 更有针对性（`src/hl_mem/observability/audit.py`、`src/hl_mem/recall/trace.py`、`src/hl_mem/api/server.py`）。

Hindsight 领先在 LLM 专项运营面：span 父子关系、provider/model、token 明细、健康检查、按时间桶聚合和 Bank 维度。

### 当前场景的实际影响

当前 audit 的 `detail_json` 可以临时承载字段，但没有稳定 schema 和查询接口。`LLMResponse` 只有 total tokens，`LLMClient` 正常调用本身也未统一 emit。结果是召回链路可解释，生成链路却难以进行成本和可靠性分析。

此外，`AuditLogger` 是 best-effort 同步写 SQLite，失败只计 dropped；这符合不阻塞主流程的目标，但应在健康状态中显式暴露，否则“没有日志”可能被误认为“没有失败”。

### 最小可行方案

1. 新增轻量 `llm_requests` 表或稳定的 typed audit action；优先独立表，便于索引和聚合。
2. health 增加最近一次成功、最近错误、连续失败数、最近探测延迟；不要在每次 `/healthz` 调用昂贵模型。
3. 增加 hour/day 聚合查询：statuses、calls、tokens、latency P50/P95。
4. 将 audit dropped count 暴露到 health/stats。
5. 保持 SearchTrace 现有候选级设计，不为了 OpenTelemetry 形式统一而丢失领域信息；未来需要跨进程关联时再桥接 OTel。

---

## 7. 提取模型

**差距评估：小差距；两者侧重点不同，不存在绝对优劣。**  
**建议：保留 Claim 主模型，计划增强时间区间与实体提及。**

### 现状差异

HL-Mem 的 `ExtractedClaim` 包含 subject、predicate、value、qualifiers、confidence、volatility、scope、importance、canonical_attribute、canonical_slot 和 topic_tags。LLM schema 还返回顶层 entities、should_memorize 和 sensitivity（`src/hl_mem/ingest/extractors.py`、`src/hl_mem/ingest/schemas.py`）。

Hindsight 的 `ExtractedFact` 更接近自然语言事实单元：text、fact_type、occurred_start/end、entities。其优势是：

- 原文语义保真；
- 时间区间是一等字段；
- 实体及其提及更适合图关联；
- 对开放域事实不强迫映射为三元结构。

HL-Mem 的优势是：

- subject/predicate/value 更适合确定性去重、冲突键和 slot 互斥；
- scope/importance 直接驱动 TTL；
- canonical slot + tags 直接服务召回和生命周期；
- qualifiers 可表达条件化事实。

### 当前场景的实际影响

HL-Mem 的整个冲突、supersede、retention 和检索架构都围绕 Claim 模型建立。替换为自由文本 Fact 会弱化确定性逻辑并导致大范围改造，不值得。

真正的缺口是当前 claim 主要把事件的 `occurred_at` 作为单点 `valid_from`，没有显式的事实发生区间；顶层 entities 也没有成为每条 claim 的结构化实体提及。对“2025 年至 2026 年在某公司任职”或“一周内使用某配置”之类事实，qualifiers 能表达但缺少统一语义和校验。

### 最小可行方案

采用超集而非替换：

- 保留 subject/predicate/value、slot/tags、scope/importance；
- 增加可选 `occurred_start` / `occurred_end`，规范映射到 `valid_from` / `valid_to`；
- 增加每条 claim 的 `entities`，至少包含 normalized entity id、原始 mention 和 role；
- 可选保留 `source_text` 或 evidence span 引用，而不是把自由文本作为唯一事实表示；
- 为时间区间冲突、开放区间和单点事件增加 schema 校验。

结论：HL-Mem 模型更适合当前强生命周期目标，Hindsight 的时间区间和实体标注值得吸收。

---

## 8. 合并机制

**差距评估：中等差距，当前影响：小到中等。**  
**建议：计划做显式 scope 与 dry-run；暂不追求复杂分区调度。**

### 现状差异

HL-Mem 的 `ConflictConsolidator`：

- 按 namespace 读取活跃且有 embedding 的 claims；
- 仅比较同 canonical slot 或同 subject 的 pair；
- 余弦在 `[0.72, 0.95)` 灰区时交给 LLM 四分类；
- 通过 pair key + embedding signature 防止重复判定；
- 使用置信度阈值、CAS 检查和事务应用结果；
- 支持内部 `dry_run` 参数和 watermark/batch size。

这比“cosine + LLM”更完整，已有幂等和并发安全基础（`src/hl_mem/workers/consolidate.py`、`storage/migrations/007_supersede_and_consolidation.sql`）。

Hindsight 的 `ConsolidationRequest` 和 `ObservationScope` 把“对哪些 tag scope 执行合并”提升为显式 API/操作模型，可以先查询分区及数量，再定向运行。这改善了成本控制、可解释性和人工运维。

### 当前场景的实际影响

514 claims 下 O(n²) 的最坏 pair 扫描仍可接受，且 HL-Mem 已通过同 slot/subject、watermark、batch 限制候选。当前更大的问题不是速度，而是控制面：

- 定时任务 payload 固定为空；
- scope 只有 namespace，没有 tag/slot 分区；
- dry-run 没有公开 API/CLI 契约；
- 没有扫描量、候选量、已处理量的持久化进度；
- watermark 语义按 `recorded_from > watermark` 取行，可能漏掉“旧 claim 与 watermark 后新 claim”的跨边界配对，需要专门测试和明确设计。

### 最小可行方案

定义 `ConsolidationScope(namespace, canonical_slots?, topic_tags?)` 和 `ConsolidationRequest(scope, dry_run, limit)`；先提供 CLI 或管理 API：

- 列出 scope 及 active claim 数；
- dry-run 只返回候选统计和预计 LLM 调用数；
- job payload 持久化 scope；
- 每批更新 progress；
- 默认 scope 保持 `namespace=default`，不改变当前定时行为。

在扩展 ObservationScope 前，先验证 tag 分区不会阻止本应跨 tag 合并的事实。

---

## 9. 多租户 / 多库

**差距评估：大差距（若以 SaaS 为目标），当前影响：无到小。**  
**建议：强隔离暂缓；现在修正语义并保留演进边界。**

### 现状差异

HL-Mem 的 claims 和若干 experience 表有 `namespace_key`，event 有 `tenant_id`，多数召回和写入查询会过滤 namespace。但源码注释明确指出它只是软标签，不是授权或完整隔离边界；后台维护、策略归纳、归档及部分 API 上下文仍使用 `default`（`src/hl_mem/api/schemas.py`、`src/hl_mem/application/ingest.py`）。

Hindsight 的 Bank 是资源边界：每个 Bank 可有独立配置、向量索引和 LLM 行为，配套健康、统计和管理接口。它不仅是 SQL 的一个过滤列。

### 当前场景的实际影响

solo 单机没有租户间数据泄露问题，也不需要为每个 namespace 建索引或维护独立模型配置。现在实现 Bank 会显著增加：

- 授权与身份传播；
- 所有后台任务的 scope 正确性；
- 配置继承和缓存；
- 备份、删除、配额和统计边界；
- 向量/FTS 索引策略。

但继续把软标签命名为 tenant/namespace，容易让调用方误判安全属性。

### 最小可行方案

1. 对外明确 `namespace` 是检索分区，不是安全租户。
2. 引入不可伪造的内部 `MemorySpaceContext`，逐步让 recall、ingest、worker、retention、policy、audit 都显式接受它；当前只实例化 default。
3. 加跨 namespace 泄露测试和维护任务作用域测试。
4. 只有出现两个需要独立权限/配置/删除生命周期的真实用户或 agent 时，再升级为强隔离 Bank：
   - 所有表带 bank_id 且有复合约束；
   - 每个 job 固定 bank_id；
   - 配置按 bank 解析；
   - API 从认证上下文获得 bank_id，而不是信任请求体；
   - 再决定共享索引过滤还是 per-bank 索引。

不要把“已有 namespace_key”当作多租户已经完成。

---

## 10. 长任务管理

**差距评估：中等差距，当前影响：中等。**  
**建议：应该做轻量进度模型。**

### 现状差异

HL-Mem 的 job 系统已有不少可靠性机制：

- pending/running/succeeded/dead 状态；
- lease、lease token、超时重领；
- attempts/max_attempts；
- idempotency key；
- worker polling 和维护调度；
- job 状态计数及错误记录。

但 `jobs` 只保存 payload、状态、lease、attempts 和 last_error；API 只返回聚合计数。任务处理函数可能在返回值里产生统计数据，但不会持续写入 `stage/processed/total/detail`（`src/hl_mem/storage/jobs.py`、`src/hl_mem/workers/worker.py`、`src/hl_mem/api/server.py:310`）。

Hindsight 的 `OperationProgress` 将长任务进度作为稳定契约，调用者可以区别“仍在工作”“卡住”“哪个阶段慢”。

### 当前场景的实际影响

日常 514 claims 时多数批处理很快，但真实 LLM 调用可能持续数十秒，多个 consolidation/dedup pair 会达到分钟级。当前只能看到 running，无法判断是否推进，也无法合理估算完成时间。出现挂起时，操作者只能查 audit 或数据库。

进度也是未来 dry-run、批量导入、backfill 和定向 consolidation 的共同低侵入基础，因此不应等到数据量大才补。

### 最小可行方案

给 jobs 增加：

- `stage TEXT`；
- `processed INTEGER`；
- `total INTEGER NULL`；
- `progress_detail_json TEXT`；
- `heartbeat_at TEXT`。

在 `JobRepository.update_progress(job_id, lease_token, ...)` 中以 lease token 做 CAS；worker 在阶段切换或每 N 项更新，避免每条记录都写 SQLite。增加 `GET /v1/jobs/{id}` 返回状态、进度、attempts、last_error 和时间戳。首批只接入 consolidation、dedup 和 backfill，短任务无需上报细粒度进度。

---

## 补充借鉴方向

以下能力不在十个主维度中，但成本低、价值明确：

### Dry-run extraction

HL-Mem 的 consolidation 内部已有 dry-run，但提取没有公开的“不落库测试”入口。Hindsight 的 `DryRunExtract` 对 prompt、schema、模型升级和自定义 instructions 的回归测试很有价值。

建议增加一个只调用 extractor、返回 claims + usage + schema diagnostics 的管理接口/CLI，默认不写 event、claim、evidence 或 audit 业务记录；仍记录安全的 LLM operation trace。自定义 instructions 应有长度和权限限制，不能直接混入生产系统 prompt。

### 线程池限制

当前 HL-Mem 主要使用远程 embedding/reranker，Python cosine 也没有引入 numpy/torch，因此 Hindsight 的 cgroup-aware OpenBLAS/OpenMP/MKL 限制当前价值很低。若未来加入本地 SentenceTransformer、FAISS 或 numpy 批量 cosine，再在导入这些库前设置线程上限；现在暂缓。

---

## 汇总表

| 维度 | 架构差距 | 当前 514 claims 影响 | 决策 | 最小可行方案 |
|---|---|---:|---|---|
| 向量检索 | 大 | 小 | 计划边界，ANN 暂缓 | `VectorSearchBackend` + 行数/延迟阈值 |
| 全文检索 | 中 | 中 | 应该做 | 中文 eval + tokenizer/检索后端可替换边界 |
| Reranker | 中 | 小 | 计划做 | provider registry，先保留单一真实实现 |
| LLM 管理 | 中 | 中 | 可观测性现在做，Router 暂缓 | 调用账本 + 可选 primary/fallback |
| 配置系统 | 中 | 小 | 计划做 | 标注 static/memory-space/request 作用域 |
| 可观测性 | 中 | 中 | 应该做 | LLM spans、健康、token/latency 聚合 |
| 提取模型 | 小 | 小到中 | 计划增强 | Claim 超集增加时间区间和实体提及 |
| 合并机制 | 中 | 小到中 | 计划做 | scope + dry-run + job payload/progress |
| 多租户/多库 | 大 | 无到小 | 暂缓 | 先引入单实例 `MemorySpaceContext` |
| 长任务管理 | 中 | 中 | 应该做 | jobs 持久化 stage/processed/total/detail |

## 优先级排序

### P0：当前就做

1. **LLM 调用账本与 span 级观测**  
   低侵入地解决成本、失败定位、模型升级验证和未来 fallback 的数据基础。它同时改善维度 4 和 6。

2. **中文全文检索评测**  
   先证明差距再选 tokenizer/后端。当前中文语料下，它比 ANN 更可能直接影响召回质量。

3. **Job 进度与 heartbeat**  
   为 consolidation、dedup、backfill、未来导入和 dry-run 建立共同控制面。

### P1：近期计划

4. **Dry-run extraction**  
   让 prompt、schema、provider/model 调整具备安全试验面，并复用 P0 的 LLM 账本。

5. **显式 ConsolidationScope**  
   支持按 namespace/slot/tags 预估和定向运行，同时验证 watermark 边界。

6. **Reranker provider registry**  
   只建立清晰扩展点；有离线、成本或隐私需求时再接一个本地或 TEI 后端。

7. **提取模型增加时间区间与实体提及**  
   保留现有 Claim 和双时间语义，以兼容方式吸收 Hindsight 的优点。

### P2：由指标或真实需求触发

8. **向量后端替换/ANN**  
   由 namespace 内向量规模、dense P95 和内存数据触发。优先本地低运维方案，服务化后再考虑 pgvector。

9. **分层配置与 MemorySpace 强隔离**  
   由第二个真实隔离单元触发，而不是由表中已有 `namespace_key` 触发。

10. **完整多模型 Router、Bank 独立索引、cgroup 线程管理**  
    分别由多模型运营、百万级/多租户检索、本地 ML 后端引入触发。

## 最终判断

HL-Mem 当前不应沿着 Hindsight 的部署形态做一次“缩小版复刻”。最合适的借鉴方式是：

- 保留 SQLite、全扫向量、Claim 模型和本地优先的简单运行面；
- 引入 Hindsight 已验证的控制面思想：操作级作用域、调用级观测、健康与统计、dry-run、持久化进度；
- 先用中文召回指标和阶段延迟决定何时替换检索后端；
- 在真正出现第二个安全/配置隔离单元前，不把 namespace 扩张成 Bank。

这样既能补齐当前最影响诊断和迭代效率的短板，也不会为 514 claims 支付面向 SaaS 和百万级数据的架构税。
