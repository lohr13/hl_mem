# hl_mem v0.16.1 召回改进实施方案

> 状态：提案（不包含实现）  
> 日期：2026-07-29  
> 适用规模：当前约 452 条 active claim、52 MB SQLite；面向开源个人部署

## 1. 结论与范围

本轮建议进入后续实施队列的有五项：

1. 建立可重复的召回质量基线与发布门禁；
2. 将现有 shadow answerability 演进为可选的低相关结果截断；
3. 完成可回答式 `index_text` 的存量回填与受控启用；
4. 增强会话指代查询的上下文化改写；
5. 将 dense 全量扫描的逐条纯 Python cosine 改为 NumPy 分批矩阵计算。

其中第 1 项是质量基础设施，第 2、3 项是近期最可能直接改善 precision/recall 的工作，第 4 项只在有 `session_id` 的对话式调用中 opt-in；第 5 项只优化等价计算的性能，不改变召回算法和排序语义。现有 Tag 独立通道和 query expansion 不应直接默认开启或继续堆规则，而应作为上述回归框架内的受控实验。

本方案明确不做 ANN、学习排序、分层记忆，也不增加新的常驻排序因子。`staged_pipeline.py` 已声明排序因子冻结；任何权重调整必须先由固定评测证明无退化。NumPy 优化仍是精确全量扫描，不属于 ANN。

## 2. 已确认的当前实现

- `RecallService.recall()` 生成原查询 embedding，可选调用 `QueryExpander`，再进入 `hybrid_claims()`；候选上限为 `min(200, max(limit * 5, recall_candidate_floor))`。
- `staged_pipeline._collect_candidates()` 对每条原始/扩展查询分别执行 FTS 与 dense 检索；Tag soft boost 默认开，独立 Tag channel 默认关。
- `_filter_and_score()` 以加权 RRF 形成 semantic 特征，再叠加 recency、access、confidence、importance、utility；slot hint 是固定 `0.05` soft boost。
- `_rerank()` 成功时使用 `0.8 * reranker + 0.2 * prior`，失败时回退到先验排序；`_finalize()` 只按 `limit` 截断，不按相关性或分数断层剔除尾部。
- `RecallService._answerability()` 已返回 `no_evidence / low_confidence / supported`，但目前是 shadow 信号，不改变 `results`。其规则使用 FTS 命中、dense 分、slot、Top-1/Top-2 margin 与 reranker raw score。
- 服务实际结果已包含 `answerability`、`score_path`、`reranker_raw_score`，但 `api/schemas.py` 的 `RecallOutput` / `ClaimOutput` 尚未声明这些字段。
- `build_index_text()` 已支持 `legacy / value_only / natural`；migration 031 已有 `index_text` 列。当前 `natural` 仅为“subject：value”，存量数据不会因切换配置自动重建索引文本和 embedding。
- `RecallInput` 有 `session_id`，但 REST 调用没有把它传入 `RecallService.recall()`，服务方法也没有该参数；因此“这个、上次、之前”等指代查询只能脱离会话上下文做 LLM 改写。
- `Settings()` 的 `query_expansion_mode` 默认是 `off`，而 `Settings.from_env()` 在未设置环境变量时使用 `auto`，开源用户的实际默认行为不一致。
- dense 主路径不是由 `staged_pipeline.py` 直接调用 `core.vector.cosine_similarity()`：管线委托 `ClaimRepository.search_claims_vector()`，后者先取出时间视图内全部 embedding，再在排序 key 中对每条记录调用 `cosine_similarity()`。该函数对查询和目标 BLOB 都执行 `struct.unpack()`，并用三个纯 Python 逐元素循环计算两边范数和点积；同一查询向量也会被重复解包、重复计算范数。
- `staged_pipeline.py` 另有一条不同的 cosine 调用：`fold_similar_claims()` 先用 `normalized_vector()` 解包并归一化候选，再用 `normalized_cosine_similarity()` 做纯 Python 点积。它最多受 `dedup_candidate_limit` 约束，且默认 `dedup_threshold=0` 时不执行，不是 dense 通道的首要热点。
- `VectorBackend`、`HL_MEM_VECTOR_BACKEND` 和 `VectorSearchBackend` 协议已经存在，但当前枚举只有 `sqlite_scan`，`RecallService` 仍直接构造 `ClaimRepository`，配置尚未驱动后端选择。加入独立后端的上层接口接缝已具备，但真正接线仍需少量工厂/注入工作。
- NumPy 当前不在项目依赖中。Hermes 对纯 Python 热点和数量级收益方向的判断成立，但“只换一个函数”低估了批量 API、依赖、异常兼容、内存峰值和后端接线的工作。

## 3. 改进项 A：固定回归集、指标分层与发布门禁

### 问题描述

当前已有 `tests/eval/datasets/recall_v2.jsonl`（50 条）及快照构建能力，但现有测试主要保护 API 合同与快照只读性，尚未形成可比较的排序质量基线。没有稳定基线时，relevance cutoff、index text、Tag channel 或扩展策略都无法证明收益，也违背排序因子冻结约束。

### 当前代码位置

- `tests/eval/datasets/recall_v2.jsonl`
- `tests/eval/datasets/recall_v2.manifest.json`
- `tests/eval/test_recall_eval.py`
- `tests/eval/fixtures/build_snapshot.py`
- `src/hl_mem/recall/trace.py::SearchTrace`
- `src/hl_mem/application/recall.py::RecallService.recall`

### 改造方案

1. 将数据集按 `answerable`、`no_answer`、`exact_entity`、`broad_topic`、`preference`、`historical`、`coreference` 分层；保留当前 50 条 ID，新增样例只追加。
2. 每条样例记录稳定的期望 claim ID（快照内可绑定时）、允许的等价 ID 集、禁止 ID/状态、最低相关等级以及可选的 Top-k 关键词约束。关键词仅作诊断，不能替代 ID/人工相关性标签。
3. 增加独立评测 runner，输出 Recall@1/5、MRR、nDCG@5、Top-3 precision、no-answer precision/recall、HTTP success rate、p50/p95，并按上述 query slice 分组。
4. 保存版本化 baseline 和运行元数据：数据库快照哈希、数据集哈希、配置 snapshot、embedder/reranker 模型及是否降级。真实 provider 的延迟与质量报告和确定性 fake 单测分开。
5. 发布门禁先采用“相对不退化”：总体 MRR、Recall@5、no-answer precision 不得低于基线容差；任何 slice 的明显退化必须人工批准。小样本阶段不使用统计学习或自动拟合权重。
6. 为后续实验定义单变量矩阵：baseline、cutoff、answerable index、context rewrite、tag channel；每次只打开一个变量。NumPy 只做结果等价性与性能基准，不作为质量变量。

### 新增/修改配置参数

运行器配置，不进入服务运行时 `Settings`：

- `dataset_path`
- `snapshot_path`
- `baseline_path`
- `top_k`（建议评测 1/3/5）
- `max_metric_regression`（门禁允许的绝对退化量）
- `provider_mode`（`fake` / `real`，仅评测脚本参数）

### 影响范围

- 新增/扩展 `tests/eval/`；不改变现有 unit tests。
- 可能需要更新数据集 manifest 与基线文件。
- `SearchTrace` 若缺少评测所需的 raw channel score，只增诊断字段。

### 验收标准

- 同一 snapshot、同一配置连续运行两次，排序指标完全一致（外部 provider 延迟除外）。
- 50 条现有样例全部被 runner 消费，且至少 30% 为明确 no-answer/hard-negative。
- 报告能分别展示 reranker applied 与 fallback，不能混合解释两种分数量纲。
- 人为交换一个期望 Top-1 后门禁必定失败；恢复后通过。
- runner 不修改源数据库，输出中不包含 claim 原文之外的敏感调试数据。

### 工作量估计

M（约 2–4 人日，主要成本是标注与复核）。

### 依赖关系

无。它是 B、C、D 的前置依赖。

## 4. 改进项 B：可选的 relevance gate 与尾部截断

### 问题描述

当前精确查询可能 Top-1 正确、Top-2/3 明显无关；`_finalize()` 仍返回到请求 `limit`。现有 `answerability` 只评价整次查询，而且规则中的最终分、RRF 分和 reranker 分量纲不同，不能直接用统一 `score >= x`。这会降低上下文 precision，并让下游 Agent 把噪声当证据。

### 当前代码位置

- `src/hl_mem/application/recall.py::RecallService._answerability`
- `src/hl_mem/application/recall.py::RecallService.recall`
- `src/hl_mem/recall/staged_pipeline.py::_rerank`
- `src/hl_mem/recall/staged_pipeline.py::_finalize`
- `src/hl_mem/recall/trace.py::SearchTrace`
- `src/hl_mem/recall/ranking.py::blend_reranker_score`
- `src/hl_mem/api/schemas.py::{ClaimOutput, RecallOutput}`

### 改造方案

分两阶段上线：

1. **observe 模式**：保持结果不变，但对每个候选计算 `relevance_decision`。优先使用 reranker raw score；reranker fallback 时使用“通道证据组合”，包括 FTS 是否命中、dense 原始 cosine、是否多通道命中、slot hint 是否匹配。不要以 final score 作为跨路径统一阈值。
2. **enforce 模式**：先保留 Top-1，再从 Top-2 开始应用逐项门槛和相对断层规则；低于门槛的候选及其后续尾部被截断。Top-1 也无证据时返回空 `results`，`answerability=no_evidence`；证据弱但未达到空结果标准时标记 `low_confidence`。
3. preference、historical 与 procedure 分开配置。首版只对普通 claim 的 `CURRENT_STATE` 启用 enforce；其他 intent 保持 observe，避免误删历史链和跨类型 Experience。
4. 把 decision reason 写入 `SearchTrace.filter_reasons`，例如 `below_reranker_floor`、`below_dense_floor`、`relative_score_drop`、`query_no_evidence`。
5. API 只增字段：`RecallOutput.answerability`；`ClaimOutput.score_path`、`reranker_raw_score`，以及可选 `relevance` 诊断对象。现有 `results/score/features` 语义不改。非 debug 响应可只返回 query 级 `answerability`，候选细节留在 debug trace。
6. `_record_access()` 和 feedback exposure 只能记录最终真正返回的结果；observe 模式仍按现状记录。

### 新增/修改配置参数

- `HL_MEM_RELEVANCE_GATE_MODE=off|observe|enforce`，默认 `off`
- `HL_MEM_RELEVANCE_RERANKER_FLOOR`，仅 applied 路径使用
- `HL_MEM_RELEVANCE_DENSE_FLOOR`，仅 fallback 组合规则使用
- `HL_MEM_RELEVANCE_RELATIVE_DROP`，控制相邻结果断层
- `HL_MEM_RELEVANCE_KEEP_TOP1=true|false`，默认 `true`；仅候选存在基本信号时有效
- 可选 `HL_MEM_RELEVANCE_INTENTS=current_state`，首版只允许白名单 intent

所有阈值必须在 `Settings.validate()` 校验到 `[0, 1]`，并加入非敏感 snapshot。

### 影响范围

- `tests/unit/test_v015_recall_monitoring.py`
- `tests/unit/test_search_trace.py`
- `tests/unit/test_recall_score_output.py`
- `tests/unit/test_reranker.py`
- `tests/unit/test_extended_recall.py`
- `tests/unit/test_recall_context.py`
- API/OpenAPI 与 MCP 契约快照
- feedback/exposure 与 access count 相关测试
- 改为 enforce 后，依赖固定返回 `limit` 条结果的调用方测试

### 验收标准

- `off` 模式与 v0.16.1 对同一快照逐 ID、逐顺序一致。
- `observe` 模式结果一致，只新增 trace/响应字段。
- `enforce` 模式在固定集上显著提高 Top-3 precision，Recall@1 不退化，Recall@5 退化不超过预设容差。
- no-answer precision 达到评测集约定目标（首轮建议不低于 0.90），且无答案请求能返回空结果。
- reranker error/empty fallback 不会错误套用 reranker 阈值。
- `total == len(results)`，被截断项不产生 access 或 exposure。

### 工作量估计

M（约 3–5 人日，含 observe 数据复核）。

### 依赖关系

依赖 A。建议先运行至少一轮 observe，再允许 enforce。

## 5. 改进项 C：可回答式 index_text 与存量安全回填

### 问题描述

当前 `legacy` 索引文本包含 subject、predicate、value、slot、tags，容易把技术元数据词当作正文；`natural` 又只生成“subject：value”，缺少 predicate/slot 的自然语言表达。短 value（如路径、型号、provider 名）常能精确命中，但宽查询缺少“谁的什么属性是什么”的完整语义，dense 与 reranker 都可能失去关系信息。仅改变新写入配置会造成新旧 claim 索引语义不一致。

### 当前代码位置

- `src/hl_mem/domain/claims/claim.py::build_index_text`
- `src/hl_mem/application/ingest.py` 中 claim 写入与 embedding 构建调用点
- `src/hl_mem/storage/claims.py::{insert_claim, search_claims_fts, search_claims_vector}`
- `src/hl_mem/storage/migrations/031_claim_index_text.sql`
- `src/hl_mem/settings.py::Settings.index_text_mode`
- `tests/unit/test_v015_recall_monitoring.py`

### 改造方案

1. 新增 `answerable` 模式（保留三个旧 mode），以确定性模板生成：subject + canonical slot/predicate 的可读标签 + value；受控 tags 只用于无法表达 slot 的补充，不直接拼接全部英文标签。
2. 模板必须来自 `SLOT_REGISTRY`，无注册 slot 时降级为 `subject + predicate + value`；禁止调用 LLM，保证开源部署可复现、无额外费用。
3. 编写独立、可恢复的 backfill worker/CLI：分批读取 active claim，生成新 `index_text` 与 embedding；采用 compare-and-set，只有源字段/旧 index hash 未变化才写回，避免覆盖并发更新。
4. 回填支持 `dry-run`、进度游标、失败重试与摘要审计。真实 embedding 调用使用现有客户端的 retry/timeout；失败批次保留旧索引，不做半更新。
5. FTS 与 dense 必须在同一 claim 更新中切换到同一版 index text。SQLite trigger 已监听 `index_text`，但仍要验证更新原子性和 FTS 一致性。
6. 先在评测 snapshot 上构建影子副本进行 A/B；正式环境默认仍为 `legacy`，只有显式配置并完成 backfill 后才切换。

### 新增/修改配置参数

- 扩展 `HL_MEM_INDEX_TEXT_MODE=legacy|value_only|natural|answerable`，默认保持 `legacy`
- `HL_MEM_INDEX_BACKFILL_BATCH_SIZE`，正整数
- `HL_MEM_INDEX_BACKFILL_MAX_ATTEMPTS`
- `HL_MEM_INDEX_TEXT_VERSION` 或持久化的生成版本，用于幂等与审计

路径、模型、超时继续复用已有 Settings/provider 配置，不在脚本硬编码。

### 影响范围

- claim 写入与 embedding 单测
- `test_v015_recall_monitoring.py`
- FTS trigram、dense、migration upgrade 与历史数据库 fixture
- 去重相关测试：回填只改检索 embedding，不得重算 `fact_hash/conflict_key`
- DB 大小与外部 embedding 调用成本；452 条当前规模可控，但必须显示 dry-run 估算

### 验收标准

- `legacy/value_only/natural` 输出与 v0.16.1 完全兼容。
- `answerable` 对 registry 中每个 operational slot 有确定性、非空输出，且不泄漏内部 JSON 表示。
- dry-run 零写入；重复执行 backfill 幂等；模拟并发更新时 CAS 跳过而非覆盖。
- 回填后 `claims.index_text`、FTS 内容和 embedding 均对应同一 index version；失败批次不出现新文本配旧向量。
- 固定回归集上 broad-topic slice 的 Recall@5/MRR 提升，总体和 exact-entity slice 不退化超过门禁容差。
- 记录回填条数、跳过数、失败数、provider 消耗和预计/实际 DB 增量。

### 工作量估计

L（约 5–8 人日，主要风险在原子回填、历史数据库与真实 embedding A/B）。

### 依赖关系

依赖 A；不依赖 B。建议在 B 的 observe 阶段独立评测，严格控制单变量。

## 6. 改进项 D：会话感知的指代查询改写

### 问题描述

`RecallInput.session_id` 当前没有进入 `RecallService`，而 `QueryExpander.trigger_for()` 会因“这个、上次、之前”等词触发 coreference 扩展，却只把原 query 发给 LLM。缺少会话先行文本时，模型无法可靠消解指代，可能产生空扩展或臆造实体。个人 AI 记忆系统的真实查询大量依赖对话上下文，这是高价值但可控的缺口。

### 当前代码位置

- `src/hl_mem/api/schemas.py::RecallInput.session_id`
- `src/hl_mem/api/server.py` 的 `/v1/recall` 调用
- `src/hl_mem/mcp/server.py` 的 recall 调用
- `src/hl_mem/application/recall.py::RecallService.recall` 与内部 `expand_for`
- `src/hl_mem/recall/query_expansion.py::{trigger_for, expand, _request}`
- `src/hl_mem/storage/events.py::EventRepository.get_recent_events`（当前只按 `session_id` 与游标过滤，未按 namespace 过滤）

### 改造方案

1. 将可选 `session_id` 从 REST 传到 `RecallService.recall()`；MCP 契约只新增可选参数，不改变旧调用。
2. 仅在 `query_expansion_mode != off`、触发原因为 `coreference` 且提供 `session_id` 时，读取同 namespace/session 的最近少量事件。必须扩展 `get_recent_events()` 的签名和 SQL，同时按 `namespace_key`、`session_id` 与游标过滤；不能依赖当前仅按 session 的查询来声明 namespace 隔离。
3. 构造最小上下文：只取最近用户/助手文本，按字符或 token 预算从新到旧截断；不把 evidence、内部 metadata 或其他 session 数据送给 provider。
4. `QueryExpander` 的结构化输出仍只返回查询文本；prompt 明确要求消解指代但不得新增上下文中不存在的事实。扩展失败、超时、session 不存在时退回原查询。
5. trace 只记录 `context_event_count`、截断状态与上下文 hash，不记录上下文原文。
6. 统一默认值：`Settings.from_env()` 未设置 `HL_MEM_QUERY_EXPANSION_MODE` 时改为 `off`，与 dataclass 一致。考虑已有部署可能依赖当前隐式 `auto`，该变化应在 release note 标为行为兼容修正，并允许用户显式设 `auto` 恢复。

### 新增/修改配置参数

- `HL_MEM_QUERY_EXPANSION_MODE` 默认统一为 `off`
- `HL_MEM_QUERY_CONTEXT_MODE=off|coreference`，默认 `off`
- `HL_MEM_QUERY_CONTEXT_MAX_EVENTS`，正整数
- `HL_MEM_QUERY_CONTEXT_TOKEN_BUDGET`，正整数

继续复用现有 expansion timeout、total timeout、token ceiling、max concurrency；上下文读取不得延长 provider 的绝对 deadline。

### 影响范围

- `tests/unit/test_query_expansion.py`
- `tests/unit/test_recall_context.py`
- API/MCP 契约快照与 server adapter 测试
- Settings 默认值、校验和 snapshot 测试
- EventRepository 会话读取测试
- provider prompt snapshot/结构化输出测试

### 验收标准

- 默认配置下，与 v0.16.1 的显式 `query_expansion_mode=off` 行为一致，不发生外部 LLM 调用。
- 无 `session_id`、未知 session、上下文读取失败或 provider 超时时均返回原查询结果，不产生 HTTP 500。
- coreference slice 的 Recall@1/MRR 相比“无上下文 auto expansion”提高，hard-negative 不出现跨 session 实体泄漏。
- namespace/session 隔离测试覆盖同名 session、不同 namespace；trace 与日志中无上下文原文。
- 总 expansion deadline 与并发限制仍成立，新增读取的 p95 开销满足评测预算。

### 工作量估计

M（约 3–5 人日）。

### 依赖关系

依赖 A。与 B、C、E 无代码依赖；评测时必须单独启用。

## 7. 改进项 E：NumPy 分批精确 cosine 扫描

### 问题描述

当前 dense 检索的主要 CPU 热点不是“全量扫描”这一算法在当前规模已经失效，而是 `search_claims_vector()` 对每条 2048 维向量调用纯 Python `cosine_similarity()`：查询 BLOB 被重复解包和求范数，目标 BLOB 逐条 `struct.unpack()`，范数与点积都在 Python 生成器循环中完成。候选规模增长到路线图预计的高强度用户半年 12,000–18,000 条时，这个常数开销会先于是否采用 ANN 成为实际瓶颈。

Hermes 提出的 NumPy 批量矩阵运算方向正确。它仍对可见向量做精确扫描，可在不改变 FTS/dense/RRF 接口和召回率的前提下显著降低 CPU 时间。但不能把实现描述成单独替换 `cosine_similarity()`：该标量函数还被去重、关系发现和归并等路径调用，直接改成批量函数不符合其接口；dense 热点需要在仓储搜索边界一次接收整批目标向量。

### 当前代码位置

- `src/hl_mem/core/vector.py::{unpack_vector, cosine_similarity, normalized_vector, normalized_cosine_similarity}`
- `src/hl_mem/storage/claims.py::{list_embedded, search_claims_vector, search}`
- `src/hl_mem/recall/staged_pipeline.py::_collect_candidates`（dense 调用点）
- `src/hl_mem/recall/staged_pipeline.py::fold_similar_claims`（非主 dense 热点，首版不改）
- `src/hl_mem/settings.py::{VectorBackend, Settings.vector_backend, Settings.from_env}`
- `src/hl_mem/protocols.py::VectorSearchBackend`
- `src/hl_mem/application/recall.py::RecallService.recall`（当前直接构造仓储，未按 backend 配置选择）
- `pyproject.toml` / `uv.lock`（当前无 NumPy 依赖）

### 改造方案

1. 保持 `search_claims_vector()` 的参数、返回值、双时间过滤与全量精确排序语义不变；查询向量只解码、校验和归一化一次。
2. 在 `core/vector.py` 新增明确的批量 cosine 原语，使用 `numpy.frombuffer(..., dtype="<f4")` 零拷贝读取单条 BLOB，并按批次构造二维 `float32` 矩阵；用矩阵乘法和向量化 L2 norm 一次计算一批分数。维度不一致继续抛出与当前语义等价的具体错误，零向量得分保持 `0.0`。
3. `ClaimRepository.search_claims_vector()` 在 `list_embedded()` 完成 namespace、状态和双时间过滤后调用批量原语，使用稳定的 `(score, 原顺序/ID)` 规则选出结果。首版保留精确全排序，避免把“向量化”和 Top-k 算法优化混成两个变量。
4. 分批而非一次性堆叠所有向量，限制临时矩阵峰值。2048 维 float32 每条约 8 KiB；18,000 条一次性矩阵约 141 MiB（尚未计入 Python row/BLOB 与临时数组），不能用一次性 `np.stack()` 换取不可控内存。
5. 首版把 NumPy 优化作为现有 `sqlite_scan` 的内部实现，而不是新增 ANN 后端；`HL_MEM_VECTOR_BACKEND=sqlite_scan` 的外部语义不变。已有 backend 协议与配置留给未来 `sqlite-vec` / HNSW 等真正不同的检索实现，避免为了同算法的计算内核引入无收益的后端分叉。
6. 不在首版顺带向量化 `fold_similar_claims()`、跨 subject 去重、关系发现或归并 worker；先用埋点证明各自热点后另做单变量优化。
7. 增加独立微基准和真实规模基准，覆盖 452、3,000、10,000、18,000 条 × 2048 维。Hermes 的 100–500 倍作为待验证假设，不直接写成验收承诺；分别记录纯计算时间、BLOB 解码/组批时间、完整 dense p50/p95 和峰值内存。

### 新增/修改配置参数

- 新增 `HL_MEM_VECTOR_BATCH_SIZE`，正整数，用于限制 NumPy 临时矩阵；默认值通过 `Settings` 集中定义并进入非敏感 snapshot。
- `HL_MEM_VECTOR_BACKEND` 继续只接受 `sqlite_scan`；本项不新增 `numpy_scan` 枚举值。
- NumPy 作为明确的运行时依赖写入 `pyproject.toml` 并锁定到 `uv.lock`，不做静默的可选导入或异常后退到慢路径。

### 影响范围

- 向量数学单测、仓储 dense 检索测试、维度错误与零向量测试
- 双时间、namespace、historical intent 的检索回归
- 排序并列与结果稳定性测试
- Settings 环境变量、校验和 snapshot 测试
- Windows/Python 3.11 的 NumPy wheel、安装体积与冷启动检查
- dense trace/benchmark：参与条数、解码字节量、组批/计算耗时和峰值内存

### 验收标准

- 固定输入上，新旧实现返回相同 ID 集与顺序；每条 cosine 与标量实现误差不超过预先约定的 `float32` 容差，并明确覆盖并列分数。
- 维度不一致仍失败且错误信息具体；空集合、零向量、非 2048 维合法向量行为与现状兼容。
- 452、3,000、10,000、18,000 条基准均报告 warm-up、重复次数、p50/p95、CPU 与峰值内存；10,000 条完整 dense p95 相比纯 Python baseline 至少提升 10 倍，且不超过路线图的 100 ms 目标。
- 调整 batch size 不改变结果，只影响延迟/内存；18,000 条时峰值内存不超过基准预设预算。
- FTS、RRF、reranker、access/exposure 和 `fold_similar_claims()` 的行为不变。

### 工作量估计

S–M（约 2–4 人日，包含依赖接入、批量原语、兼容性测试和跨规模 benchmark；不包含 ANN）。

### 依赖关系

实现无质量依赖，可与 A 并行；发布前需要 A 的固定快照做结果等价性门禁。它不依赖 B、C、D，也不解除 ANN 的规模/延迟触发条件。

## 8. 推荐实施顺序（拓扑排序）

```text
A 固定回归集与门禁
├── B relevance observe ──> B relevance enforce（达到门禁后）
├── C answerable index 影子 A/B ──> dry-run ──> 显式 backfill/启用
├── D session coreference A/B ──> opt-in 启用
└── E NumPy 精确扫描等价性/基准 ──> 替换 sqlite_scan 计算内核
```

推荐交付批次：

1. **批次 1：A**。冻结 snapshot、标注和 baseline，先解决“无法证明改对”的问题。
2. **批次 2：E**。先建立纯 Python baseline，再做结果等价性、跨规模延迟和内存门禁；这是 P1 性能项，不等待 10,000 条触发。
3. **批次 3：B-observe**。只增诊断和 API 字段，不改变结果；收集阈值分布。
4. **批次 4：C 影子 A/B**。验证宽查询收益；通过后才提供 backfill CLI，默认不切换。
5. **批次 5：B-enforce**。使用 A/C 后的稳定分布定阈值，先只对白名单 intent opt-in。
6. **批次 6：D**。面向明确传入 session_id 的调用方试点，保持双重 opt-in。

B 与 C 在代码上可并行，但评测不能同时改变两个质量变量。D 可独立开发，仍应在 C/B 关闭时建立自身增量结果。E 不改变质量算法，可并行实现，但必须先证明数值与排序等价，再作为后续质量实验的共同性能底座。

## 9. API 与开源兼容策略

- 所有新运行时能力默认 `off`；`answerable` index 必须显式选择并执行 backfill；relevance enforce 必须从 observe 晋级；session context 需要 expansion 与 context 两个开关。
- API 只新增可选字段/参数：`answerability`、`score_path`、`reranker_raw_score`、可选 `relevance`、MCP 的可选 `session_id`。不删除或改名现有字段。
- `score` 暂不改语义；客户端应结合新增 `score_path` 理解量纲。未来若引入校准概率，应新增 `relevance_probability`，不能复用 `score`。
- `total` 始终表示实际返回条数。enforce 截断是 opt-in 行为，并在 release note 与配置 snapshot 中可见。
- migration 031 已存在，不修改历史 migration；若需记录 index version，新增顺序 migration。
- 开源用户没有 LLM key 时，A/B/C 均可使用；D 自动降级，不能阻断基础 FTS+dense 召回。
- E 不改变 API 或存储格式；NumPy 是本地运行时依赖，版本进入 lockfile，`HL_MEM_VECTOR_BACKEND=sqlite_scan` 保持兼容。

## 10. 评估提到但本方案不建议实施的项目

### ANN / 外部向量数据库

当前不做 ANN，但立即做 E 的 NumPy 精确扫描。路线图预计高强度用户半年达到 12,000–18,000 条；在改变检索算法前，应先消除纯 Python 计算内核这个常数瓶颈。只有活跃且带 embedding 的 claims 持续超过 10,000、dense p95 连续 7 天超过 100 ms、或解码内存/CPU 压力任一成立时，才对 sqlite-vec、HNSW 等 ANN/索引方案做 Recall@50、延迟、构建、增量写入和磁盘占用的离线对照。E 达标不取消这些触发条件，E 未达标也不能绕过对照直接迁移。

### 学习排序与 answerability 校准模型

暂不做。当前标注量和真实反馈量不足，训练模型很容易拟合单一快照。先积累 A 的人工相关性标签与 B-observe 数据；在数据达到预先定义的最小规模后另立提案。

### 分层记忆 / 新增 semantic、temporal、graph channel

不做。会改变 Claim/Observation/Experience 的职责和统一 packing，架构侵入远大于当前质量收益。关系扩展已有 opt-in 能力，应先评测现有通道。

### 继续增加 ranking boost 或立即调权重

不做。当前已有 semantic、recency、access、confidence、importance、utility、tag、slot、preference 多种影响源，再加 boost 会进一步降低可解释性。先执行 A，并遵守 `staged_pipeline.py` 的排序冻结说明。

### 直接默认开启 Tag 独立通道

暂不做。该能力已经实现且默认关闭，当前规则覆盖面窄、低信息 tag 被排除，边际收益未知。把它作为 A 中的单变量实验即可；证明 Recall@5 提升且 precision 无显著退化后，再讨论按特定 intent opt-in，无需另做架构项目。

### 无条件 LLM/HyDE 扩展

不做。成本、延迟和幻觉风险不适合开源默认路径；现有受控 query rewrite 已具备 timeout/retry/circuit breaker。只补 D 的真实上下文，并保持 opt-in。

### 仅靠扩大 candidate limit

不做为独立项目。当前 active claim 只有 452 条，默认 floor 50、上限 200，候选不足并非所有失败查询的根因；盲目扩大只会提高 reranker 成本并引入更多噪声。可在 A 的单变量实验中验证，不通过则维持现状。

## 11. 整体完成定义

本提案完成不等于所有运行时能力都默认启用。整体完成的标准是：

- A 能稳定复现实验并阻止已知质量退化；
- B 至少完成 observe，enforce 只有在 no-answer 与 Top-3 precision 指标达标后才可 opt-in；
- C 提供幂等 dry-run/backfill，且 `answerable` 模式通过回归门禁；
- D 在隔离、降级、延迟和 coreference 指标上达标，默认仍关闭；
- E 在固定快照上保持结果/顺序等价，并在 10,000 条基准达到延迟目标、在 18,000 条基准满足内存预算；
- 旧 API 调用、旧 index mode、无 LLM key 的本地部署保持可用；
- 每次上线只改变一个实验变量，并保存对应配置、快照与基线。
