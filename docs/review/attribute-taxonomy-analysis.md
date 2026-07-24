# HL-Mem canonical_attribute 分类体系分析

> 评审日期：2026-07-24
> 范围：只分析现有分类、提取与消费链路，不修改代码。
> 数据口径：2026-07-24 本次评审首次查询 `var/hl_mem.db` 得到的 520 条 active 只读快照；源码以当前工作树为准。该库有后台生命周期任务，文档完成后的复核查询已变为 519 条，故本文固定使用首次完整分布，不混用后续时点。

## 结论摘要

HL-Mem 的问题不是简单的“54 类一定太多”，而是**54 个标签承担了不止一种职责，却没有向分类器提供完整定义，也没有用下游收益证明这种粒度**：

1. 54 个受控属性中，当前 active claims 只使用 35 个；按分布熵计算，有效类别数仅 8.33。
2. 当前库 520 条 active claims 中，`.other` 有 313 条（60.19%）；`fact.other` 有 240 条，占全体 46.15%，占“事实”predicate 的 94.86%。
3. LLM prompt 只列约 12 个示例属性，没有给出完整 54 项 allowlist、逐项定义、边界或反例。严格 JSON schema 只校验 `domain.slot` 格式，也没有枚举合法值。因此模型实际上没有足够信息稳定完成 54 类分类。
4. `fact.*` 同时混合了两个正交维度：
   - 主题/对象：`tool_choice`、`project_membership`
   - 语义角色/事件阶段：`capability`、`implementation`、`issue`、`cause`、`resolution`、`constraint`

   一条“某服务因路由配置错误导致 recall 500，现已修复”的事实天然跨越 issue、cause、resolution、config/network；强制单选必然产生歧义。
5. canonical_attribute 并非纯展示标签。它进入 conflict key、语义去重候选隔离、互斥冲突判断和少量 TTL 规则。因此照搬 Mem0、直接删掉该字段会损失 HL-Mem 的结构化能力；但继续把所有事实都塞进同一细粒度本体，也没有数据支持。

**推荐方案是 E：职责分离的混合本体。**保留 predicate；将 canonical_attribute 收缩为少量、可操作、可验证的“状态槽/冲突槽”，只服务精确更新、冲突和生命周期；另加可多值、可扩展的 `topic_tags` / `fact_kind` 用于检索与分析。短期先实施方案 C 的观测与 prompt 修复，建立标注集和混淆矩阵；有数据后再做 E 的 schema/migration。不要立即扩充更多 `fact.*`，也不要直接删除 canonical_attribute。

## 1. 当前体系的量化分析

### 1.1 名义规模

[`PREDICATE_ATTRIBUTE_MAP`](../../src/hl_mem/domain/claims/attributes.py#L19) 定义了 8 个 predicate、54 个 canonical attributes：

| Predicate | 属性数 | fallback |
|---|---:|---|
| 偏好 | 6 | `preference.other` |
| 使用 | 9 | `choice.tool` |
| 状态 | 7 | `state.other` |
| 身份 | 5 | `identity.other` |
| 配置 | 11 | `config.other` |
| 计划 | 6 | `plan.other` |
| 事实 | 9 | `fact.other` |
| explicit_memory | 1 | `memory.explicit` |
| **合计** | **54** | |

此外，[`ATTRIBUTE_ALLOWLIST`](../../src/hl_mem/domain/claims/attributes.py#L52) 还接受 `custom.unknown`，所以验证层实际可出现 55 个值。

这里有一个不对称设计：`使用`没有 `.other`，未知使用类默认回落到 `choice.tool`（[`attributes.py:24-27`](../../src/hl_mem/domain/claims/attributes.py#L24)）。因此“所有 `.other` 的比例”会低估使用类的模糊归类，`choice.tool` 的 39 条也不应全部解释为高置信工具分类。

### 1.2 当前数据库分布

本次使用的核心查询：

```sql
SELECT canonical_attribute,
       COUNT(*) AS n,
       ROUND(100.0 * COUNT(*) /
             (SELECT COUNT(*) FROM claims WHERE status = 'active'), 2) AS pct
FROM claims
WHERE status = 'active'
GROUP BY canonical_attribute
ORDER BY n DESC, canonical_attribute;
```

当前库有 520 条 active claims，分布如下：

| canonical_attribute | 数量 | active 占比 |
|---|---:|---:|
| `fact.other` | 240 | 46.15% |
| `config.env` | 51 | 9.81% |
| `config.path` | 45 | 8.65% |
| `choice.tool` | 39 | 7.50% |
| `config.other` | 37 | 7.12% |
| `preference.other` | 15 | 2.88% |
| `preference.tool_choice` | 12 | 2.31% |
| `plan.deadline` | 11 | 2.12% |
| `state.other` | 11 | 2.12% |
| `plan.other` | 9 | 1.73% |
| `fact.tool_choice` | 5 | 0.96% |
| `plan.evaluation` | 5 | 0.96% |
| `config.network` | 4 | 0.77% |
| `choice.database` | 3 | 0.58% |
| `choice.provider` | 3 | 0.58% |
| `fact.project_membership` | 3 | 0.58% |
| `state.service_health` | 3 | 0.58% |
| 其余 18 个已用属性 | 各 1–2 | 各 0.19%–0.38% |

54 个名义属性中：

- 已使用：35 个（64.81%）
- 零使用：19 个（35.19%）
- 非 `.other`：207 条（39.81%）
- `.other`：313 条（60.19%）
- Shannon entropy：3.058 bits
- 对应有效类别数 `2^H`：8.33

“有效类别数 8.33”不是建议只保留 8 类，而是说明当前 54 类的实际信息容量远低于名义容量：存储和分类成本按 54 类支付，数据分离效果却接近个位数类别。

### 1.3 各 predicate 内部 fallback 率

| Predicate | active | `.other` | predicate 内占比 | 已使用属性数 |
|---|---:|---:|---:|---:|
| 事实 | 253 | 240 | **94.86%** | 6/9 |
| 配置 | 145 | 37 | 25.52% | 10/11 |
| 使用 | 47 | 0 | 0%* | 5/9 |
| 偏好 | 29 | 15 | 51.72% | 3/6 |
| 计划 | 25 | 9 | 36.00% | 3/6 |
| 状态 | 18 | 11 | 61.11% | 5/7 |
| 身份 | 2 | 1 | 50.00% | 2/5 |
| explicit_memory | 1 | 0 | 0% | 1/1 |

\* `使用`的 fallback 是 `choice.tool`，不是 `.other`，不能与其他行直接比较。

题述快照为 522 active、328 `.other`（约 63%）；当前只读快照为 520 active、313 `.other`（60.19%），其中 `state.other=11`、`identity.other=1`。这说明数据库在持续变化，也说明后续评估必须固定 snapshot/version，而不能只记录手工汇总数字。

文档完成后的新一次只读复核已变为 519 active，而 `.other=313`、`fact.other=240` 未变（对应 60.31% 与 46.24%）；这是后台状态变化后的另一个时点，不用于改写上面的 520 条完整分布。

## 2. fact.other 为何占 46%

### 2.1 首要原因：prompt 没有真正描述 54 类

题目所指的 `src/hl_mem/ingest/extractor.py` 在当前工作树不存在；真实 LLM 提取实现是 [`src/hl_mem/ingest/llm_extractor.py`](../../src/hl_mem/ingest/llm_extractor.py)。

prompt 对 predicate 给出了简短定义（[`llm_extractor.py:34`](../../src/hl_mem/ingest/llm_extractor.py#L34)），但对 canonical_attribute 只给出约 12 个例子：

> `preference.ui_theme`、`preference.tool_choice`、`choice.tool`、`choice.database`、`state.service_health`、`identity.role`、`config.port`、`config.path`、`config.env`、`plan.deadline`、`fact.tool_choice`、`fact.other`

见 [`llm_extractor.py:35`](../../src/hl_mem/ingest/llm_extractor.py#L35)。没有出现在 prompt 中的属性包括 `fact.implementation`、`fact.issue`、`fact.cause`、`fact.resolution`、`fact.constraint` 等。模型被要求“不得创造新值”，却看不到完整合法集合；选择已展示的 `fact.other` 是保守且合理的行为。

同时，[Pydantic schema](../../src/hl_mem/ingest/schemas.py#L10) 只用正则检查 `domain.slot` 格式（第 17 行），没有把 54 个值作为 JSON Schema enum 发给模型。最终 allowlist 校验发生在本地后处理，而不是生成约束中。

所以当前数据不能证明“LLM 面对 54 个明确定义类别仍然分错”；更准确的结论是：**LLM 从未被完整告知这 54 类。**

### 2.2 第二原因：fact 本体的轴混杂且覆盖开发者事实不足

`fact.*` 的 8 个具体类并不是同一抽象层级：

- `capability`、`constraint`：性质
- `implementation`、`issue`、`resolution`：生命周期/事件阶段
- `cause`：论证关系
- `project_membership`、`tool_choice`：主题关系

数据库抽样中的 `fact.other` 包含：

- 架构：分层架构、REST/MCP/Worker 委托关系
- 设计决策：不引入 sqlite-vec、使用 stdlib 替代 httpx
- 算法/策略：reranker 融合公式、衰减阈值、排序权重
- 运行机制：Worker 每 2 秒轮询、服务需重启加载代码
- 依赖/兼容性：零外部依赖、测试约束
- 缺陷、原因和影响的复合陈述
- 版本/发布/退役状态

其中一些可勉强归入 `implementation`、`constraint` 或 `issue`，但大量内容确实缺少稳定的“冲突槽”。把它们全都硬分成更细的单标签，未必会改善召回或冲突检测。

### 2.3 第三原因：确定性规则覆盖窄，且是词面规则

[`ATTRIBUTE_HINTS`](../../src/hl_mem/domain/claims/attributes.py#L75) 使用 `hint in text`；[`_HIGH_CONFIDENCE_ATTRIBUTE_PATTERNS`](../../src/hl_mem/domain/claims/attributes.py#L146) 只为以下少量模式提供覆盖：

- 使用：model/provider/protocol
- 配置：env/network/path/port/model/provider
- 状态：test_suite/deployment
- 事实：implementation

事实类的高置信正则只有一个，且只匹配“已实现/新增/接入/支持/修复实现”（[`attributes.py:198-200`](../../src/hl_mem/domain/claims/attributes.py#L198)）。普通 hints 也主要依赖少量显式词（[`attributes.py:136-143`](../../src/hl_mem/domain/claims/attributes.py#L136)）。例如“采用分层架构”“职责已迁移到 application 层”“零外部依赖”可能语义上是 implementation/capability/constraint，却不命中词面。

协调逻辑（[`attributes.py:261-285`](../../src/hl_mem/domain/claims/attributes.py#L261)）只在：

1. 高置信规则命中；或
2. LLM 返回 fallback/unknown，且普通推断得到合法属性

时纠正。规则没命中就保留 `fact.other`。当前 240 条 `fact.other` 全部来自 `llm-v1`，说明不是 fake extractor 或历史空字段造成的简单迁移噪声。

### 2.4 根因排序

| 根因 | 判断 | 证据 |
|---|---|---|
| prompt 描述不足 | **首要实现问题** | 只展示约 12/54 个值；`fact.other` 被展示，其他 fact 细类未展示 |
| 本体轴混杂/领域缺口 | **首要设计问题** | 开发者事实大量是 architecture/decision/behavior/policy；现有 fact 类不在同一抽象轴 |
| 确定性规则不足 | 次要实现问题 | fact 高置信规则仅覆盖 implementation；普通规则为小型词表 |
| 类别太少 | 部分成立 | architecture/decision/requirement 等高频概念无自然位置 |
| 类别太多导致选择困难 | 部分成立，但当前不能直接验证 | 模型实际未看到完整 54 类；先修信息供给才能测类别数量效应 |

## 3. 63%（当前 60.19%）进入 `.other` 说明什么

这是**设计问题与实现问题叠加**，不能只归因一方：

- 实现层面，分类器没有完整标签定义，规则覆盖又偏窄。
- 设计层面，单一 canonical_attribute 同时承担检索标签、去重边界、冲突槽和 TTL 策略，导致分类目标不一致。

二级分类仍有局部价值。`config.env`、`config.path`、`plan.deadline`、`preference.tool_choice` 等槽已经形成明显数据簇；其中部分属性还参与确定性冲突与 TTL。问题是这种价值集中在少数“可操作槽”，并未扩展到全部 54 类。

下游代码进一步说明这一点：

- 所有属性都进入 v2 conflict key（[`conflicts.py:26-49`](../../src/hl_mem/domain/claims/conflicts.py#L26)）。
- 但只有 6 个属性被声明为 mutually exclusive（[`attributes.py:64-73`](../../src/hl_mem/domain/claims/attributes.py#L64)）。
- 只有互斥属性才查询同 conflict key 的候选（[`ingest.py:395-404`](../../src/hl_mem/application/ingest.py#L395)）。
- 语义去重要求属性相同，只有 `choice.model` 与 `config.model` 一个跨属性兼容组（[`dedup.py:23-31`](../../src/hl_mem/domain/claims/dedup.py#L23)、[`dedup.py:59-70`](../../src/hl_mem/domain/claims/dedup.py#L59)）。

这意味着 `.other` 不只是“标签不好看”：

- 大量同 subject 的无关 `fact.other` 被放进同一语义去重候选池，增加误去重风险。
- 同一语义若被分到不同细类，又会被完全隔离，增加漏去重风险。
- 绝大多数细类不触发确定性冲突，分类成本没有兑现为冲突收益。

因此不应简化成“二级分类无价值”，而应改成：**只为有明确操作语义的属性支付受控本体成本；其余主题分类用低约束、多标签机制。**

## 4. 与竞品的根本差异

### 4.1 Mem0

Mem0 当前开源 V3 prompt 明确采用 ADD-only extraction，输出自包含自然语言 `text` 和 memory links，而不是 subject/predicate/attribute 本体；旧/通用 prompt 列出若干“应记忆的信息类型”，输出仍是字符串 facts。[Mem0 官方 prompts.py](https://github.com/mem0ai/mem0/blob/main/mem0/configs/prompts.py)

需要修正题述中的一个表述：当前一手源码可以确认 Mem0 prompt 有宽泛“信息类型”和 flat text memory，也可以确认 API 对象可带 `categories[]`/metadata；但**不能从当前官方源码确认 Personal、Preferences、Work、AI/ML/Tech、Health、Finance 是固定的核心 5–6 类本体**。更稳妥的比较是：

- Mem0 的核心记忆内容是自然语言文本；
- category/metadata 是可选过滤维度，不是 conflict slot；
- V3 提取阶段只 ADD，关联已有 memories，避免在同一阶段决定 UPDATE/DELETE；
- 搜索以语义检索为主，分类错误通常降低过滤/分析质量，但不会改变事实的结构化冲突键。

Mem0 的 Memory Decay 是搜索时软权重，不删除或过滤候选，且不改 categories/metadata/embedding；Memory Expiration 则控制过期后不再出现在搜索中。[Memory Decay](https://docs.mem0.ai/platform/features/memory-decay)、[Memory Expiration](https://docs.mem0.ai/platform/features/memory-expiration)

### 4.2 Zep / Graphiti

Zep/Graphiti 与 HL-Mem 更接近：事实是 entity—relationship—entity 的 temporal edge，支持双时间、事实失效、episode provenance，以及 vector/BM25/graph 混合检索。[Graphiti overview](https://help.getzep.com/graphiti/getting-started/overview)

其分类策略不是固定的 54 个“记忆主题”，而是：

- edge name 表达关系类型，如 `WORKS_AT`、`LIVES_IN`；
- 默认可由系统生成关系名；
- 需要领域精度时，用户定义 custom entity/edge types；
- 时间有效性与关系类型是结构化一等字段。

Zep 的 facts 文档还显示，每条 edge fact 有 `created_at`、`valid_at`、`invalid_at`、`expired_at`，更新可通过失效旧 edge 表达。[Zep Facts](https://help.getzep.com/facts)

启示是：**结构化记忆需要关系/槽，但本体应服务关系推理和时间变化；领域本体最好可配置，而不是把所有主题固化进全局枚举。**

### 4.3 LangMem

LangMem 明确同时支持两种语义记忆表示：

- collection：非定长 memories，适合语义搜索；
- profile：应用特定严格 schema，适合直接查字段。

它允许用户提供 Pydantic schemas；也可不提供 schema，退回非结构化字符串。官方示例直接用 `subject/predicate/object/context` triple，但 predicate 不是全局固定枚举。[LangMem semantic memory guide](https://langchain-ai.github.io/langmem/guides/extract_semantic_memories/)、[Core concepts](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/)

启示是：结构化与非结构化不是二选一。可以把稳定、可更新的 profile/slot 与开放事实 collection 并存，这正适合 HL-Mem 的开发者记忆。

### 4.4 对比矩阵

| 系统 | 核心表示 | 分类/本体 | 冲突与时间 | 分类错误的主要代价 |
|---|---|---|---|---|
| HL-Mem | subject + predicate + value + canonical_attribute | 固定 8×54 两层单选 | 双时间、conflict key、互斥槽、证据链 | 影响去重候选、冲突槽、TTL、过滤 |
| Mem0 | 自包含自然语言 memory + metadata/link | 宽泛指导；category/metadata 可选 | V3 extraction ADD-only；后续关联/处理 | 多为过滤、分析和召回排序损失 |
| Zep/Graphiti | temporal entity-edge-entity fact | 关系名 + 可配置 entity/edge types | edge 失效、双时间、provenance | 影响图遍历、关系合并与时间推理 |
| LangMem | collection 或 profile/schema | 应用自定义 schema；可无 schema | 可配置 insert/update/delete | 取决于应用 schema，风险局部化 |

HL-Mem 与 Mem0 的根本差别确实影响分类策略：Mem0 可以容忍弱标签，因为自然语言文本仍保留完整语义；HL-Mem 把 canonical_attribute 用作操作键，错分会改变系统行为。因此 HL-Mem **不应追求更多装饰性类别，而应追求更少但更可靠的操作性槽**。

## 5. 五个方案的成本收益

### 方案 A：增加更多 `fact.*`

例：`fact.architecture`、`fact.decision`、`fact.requirement`、`fact.behavior`、`fact.algorithm`、`fact.dependency`、`fact.version`。

| 项目 | 评估 |
|---|---|
| 收益 | 能覆盖当前 `fact.other` 中明显的开发者高频主题；便于统计和过滤；短期看起来最直接 |
| 成本 | 扩充 prompt、schema enum、规则、测试、迁移/backfill；标签更多，混淆矩阵更稀疏 |
| 风险 | 继续混合“主题”和“语义角色”；复合事实仍不能单选；新类可能只是把一个大 `other` 拆成多个不可靠桶 |
| 对冲突/去重收益 | 只有新增类对应明确互斥槽或兼容规则时才有收益；否则只是改变去重隔离边界 |
| 结论 | **不建议单独采用**。只能在标注样本证明某一新槽高频、可判、且下游有操作语义后按需增加 |

### 方案 B：减少层级，改成 Mem0 式 flat category

保留自然语言 claim，使用少量 category tags，去掉 predicate→attribute 层级约束。

| 项目 | 评估 |
|---|---|
| 收益 | LLM 分类更容易；开放标签适合开发者领域演化；prompt 和维护成本下降 |
| 成本 | conflict key、dedup compatibility、TTL 和偏好 intent 都需重新设计；数据迁移范围大 |
| 风险 | flat category 仍可能把“状态槽”和“主题标签”混在一起；若 category 仍单选，只是把问题压平 |
| 对冲突/去重收益 | 会损失 subject+slot 的精确边界，除非另建 slot 字段 |
| 结论 | **不建议原样采用**；适合作为辅助 topic tag 层，而不是 canonical slot 的替代品 |

### 方案 C：保持 54 类，优化 prompt 和规则

| 项目 | 评估 |
|---|---|
| 收益 | 最低 schema 风险；可快速验证当前高 fallback 到底有多少是实现问题；兼容现有数据 |
| 成本 | 需要完整类定义、正反例、规则测试、离线标注集、分类来源审计；prompt token 增加 |
| 风险 | 即使准确率提升，本体轴混杂问题仍存在；规则词表会持续膨胀；可能把不确定错误从可见的 `.other` 变成不可见的错类 |
| 对冲突/去重收益 | 若评测准确，可改善去重隔离；但多数属性仍不参与互斥冲突 |
| 结论 | **推荐作为短期诊断与止血阶段**，不应被视为最终形态 |

关键点不是追求 `.other` 越低越好。fallback 是校准机制；若模型不确定，诚实的 `.other` 优于自信错类。目标应是标注集上的 precision/recall、冲突准确率和去重误差，而不是单一 fallback KPI。

### 方案 D：完全去掉 canonical_attribute

只保留 predicate + vector/FTS。

| 项目 | 评估 |
|---|---|
| 收益 | 提取最简单；没有标签漂移；开放事实天然适配 |
| 成本 | conflict key v2、互斥槽、semantic dedup、attribute TTL、相关测试和 migration 全部重做 |
| 风险 | 同 subject 下“端口”“模型”“主题”等事实更难精确更新；冲突判断更依赖 LLM/向量，成本和不确定性上升 |
| 对冲突/去重收益 | 明显退化；vector 相似不能可靠替代离散状态槽 |
| 结论 | **不推荐**。这会丢掉 HL-Mem 相对 Mem0 的核心结构化优势 |

### 方案 E：职责分离的混合本体（推荐）

将当前单字段拆成三种职责：

1. `predicate`：保留 8 个高层语义类型，用于路由和基本策略。
2. `canonical_slot`（可空、受控、小集合）：只表示可精确更新/冲突/生命周期的槽，例如 UI theme、response style、model、port、service health、deadline、path/env key 等。
3. `fact_kind` / `topic_tags`：开放或项目可配；可多值，用于 architecture、decision、requirement、algorithm、dependency 等检索和统计维度，不进入 conflict key。

开放事实没有稳定槽时，`canonical_slot = NULL`，而不是伪装成 `fact.other`。它仍可通过 subject + predicate + value、FTS/vector 和证据链召回。

| 项目 | 评估 |
|---|---|
| 收益 | 每个字段职责单一；保留精确冲突能力；开放事实不被强制单选；分类错误的行为影响被限制在相应层 |
| 成本 | 需要新 migration、双写/回填、查询兼容、评测；中等到高 |
| 风险 | 设计过宽会出现 tag 泛滥；slot 集合若无准入标准仍会再次膨胀 |
| 对冲突/去重收益 | conflict 只依赖高精度 slot；topic tags 不错误隔离 semantic dedup，可按 predicate/subject 再做候选控制 |
| 结论 | **最佳长期方案**，与 Zep 的关系类型 + 自定义本体、LangMem 的 collection + profile 双轨思路一致 |

## 6. 推荐路线

### 阶段 0：先建立可测基线（不迁移）

1. 固定当前 DB snapshot，分层抽样至少 200 条：覆盖各 predicate、`.other`、高频非 `.other`。
2. 人工标注：
   - predicate
   - 是否存在稳定 conflict slot
   - slot（若存在）
   - 可多选 fact kinds/topics
3. 指标至少包括：
   - predicate macro-F1
   - slot precision/recall 与 abstention rate
   - `.other`/NULL 的校准准确性
   - dedup false-positive/false-negative
   - conflict candidate precision/recall
4. 在审计中记录 `llm_attribute`、`rule_attribute`、最终值和 reconciliation reason。当前 `_attribute_reason` 在 [`llm_extractor.py:383-391`](../../src/hl_mem/ingest/llm_extractor.py#L383) 计算后被丢弃，无法从历史数据区分“LLM 选 other”与“规则回退 other”。

### 阶段 1：采用 C 验证上限

- 给模型完整 allowlist、中文定义、边界和正反例，不再只列 12 个例子。
- 将合法属性直接写进 structured-output enum，而不是只做字符串正则。
- 规则仅覆盖可高精度判定的格式（端口、路径、环境变量、URL、明确状态），避免用宽泛词强行降低 fallback。
- 对同一标注集做三组可控实验：
  1. 当前 prompt
  2. 完整定义 prompt
  3. 完整定义 + 高置信规则

只有这样才能回答“54 类本身是否导致 LLM 困难”，并遵守一次只改一个变量。

### 阶段 2：迁移到 E

slot 准入标准应同时满足：

1. 有明确、稳定、单值或可定义 qualifier scope 的语义；
2. 人工标注一致性高；
3. 下游确实需要 exact lookup、冲突、TTL 或去重边界；
4. 错分的行为代价可测试。

不满足条件的分类只能做 tag，不能进入 canonical slot。本阶段不以“把 `.other` 降到 10%”为目标，而以“可操作 slot 的 precision 足够高，开放事实不被错误约束”为目标。

## 7. 若实施推荐方案，具体代码改动点

以下是未来实施点，不在本次评审中修改。

| 文件与行号 | 改动 |
|---|---|
| [`src/hl_mem/domain/claims/attributes.py:19`](../../src/hl_mem/domain/claims/attributes.py#L19) | 将 `PREDICATE_ATTRIBUTE_MAP` 演进为小型 operational slot registry；给每个 slot 声明用途（conflict/dedup/TTL）、定义和 aliases；开放事实允许无 slot |
| [`src/hl_mem/domain/claims/attributes.py:64`](../../src/hl_mem/domain/claims/attributes.py#L64) | 将 `MUTUALLY_EXCLUSIVE_SLOTS` 与 slot registry 合并，避免“54 项 allowlist + 6 项实际操作集合”分离漂移 |
| [`src/hl_mem/domain/claims/attributes.py:75`](../../src/hl_mem/domain/claims/attributes.py#L75) | `ATTRIBUTE_HINTS` 只保留高 precision 规则；topic/fact kind 不用单值词表推断，允许多标签 |
| [`src/hl_mem/ingest/llm_extractor.py:31`](../../src/hl_mem/ingest/llm_extractor.py#L31) | prompt 分开要求 predicate、可空 canonical slot、多值 fact kind/topic tags；提供完整动态生成的 slot 定义与 abstain 规则 |
| [`src/hl_mem/ingest/llm_extractor.py:383`](../../src/hl_mem/ingest/llm_extractor.py#L383) | 保存 LLM 值、规则值、最终值和 reconciliation reason 到 audit，支持混淆分析 |
| [`src/hl_mem/ingest/schemas.py:10`](../../src/hl_mem/ingest/schemas.py#L10) | schema 增加 nullable slot 与多值 tags/kind；slot 使用动态/显式 enum，而非仅正则 |
| [`src/hl_mem/application/ingest.py:344`](../../src/hl_mem/application/ingest.py#L344) | claim draft 分别验证 slot 和 tags；只有 slot 参与 attribute TTL 与 conflict key |
| [`src/hl_mem/application/ingest.py:395`](../../src/hl_mem/application/ingest.py#L395) | `_find_resolution` 明确仅对 operational slot 查询冲突候选；无 slot 走 exact/semantic dedup |
| [`src/hl_mem/domain/claims/conflicts.py:26`](../../src/hl_mem/domain/claims/conflicts.py#L26) | conflict key 接受 nullable slot；无 slot 不生成可用于互斥冲突的共享键，避免所有开放事实落同一 `fact.other` 槽 |
| [`src/hl_mem/domain/claims/dedup.py:23`](../../src/hl_mem/domain/claims/dedup.py#L23) | 重设候选兼容策略：operational slots 严格隔离；无 slot 的开放事实按 subject+predicate 检索后用 embedding/value 判定；tags 不作硬隔离 |
| [`src/hl_mem/config.py`](../../src/hl_mem/config.py) | 将 `ATTRIBUTE_TTL_DAYS` 改为 slot policy registry 或同步改名，避免 tag 被误用为生命周期规则 |
| [`src/hl_mem/storage/migrations/`](../../src/hl_mem/storage/migrations/) | 新增不可变 migration：增加新字段、双写期回填和索引；不得修改已有 migration |
| [`tests/unit/test_attribute_map.py:13`](../../tests/unit/test_attribute_map.py#L13) | 增加 slot abstention、完整 registry、定义唯一性和规则 precision 样例 |
| [`tests/unit/test_llm_extractor.py:94`](../../tests/unit/test_llm_extractor.py#L94) | 测试完整 enum/定义进入 structured output，测试复合事实多标签与无 slot |
| [`tests/unit/test_conflict.py`](../../tests/unit/test_conflict.py) | 验证只有 operational slots 触发冲突；开放事实不会因共享 fallback 槽互相冲突 |
| [`tests/unit/test_dedup.py`](../../tests/unit/test_dedup.py) | 加入 `.other`/NULL 开放事实的误合并、跨标签同义事实和 slot 隔离用例 |

## 8. 最终判断

1. **54 类是否太多？** 对当前 prompt、样本量和下游用途而言，名义上过多；但真正问题是其中大部分不是明确的 operational slots。不能仅凭与 Mem0 的 5–7 个宽泛信息类型对比得出结论。
2. **`fact.other` 46% 的真正原因？** 首先是 prompt 没有展示完整 fact taxonomy；其次是 fact 本体混轴且缺少开发者高频概念；再次是词面规则覆盖窄。不是单纯“事实子类太少”。
3. **60%+ `.other` 是否证明二级分类无价值？** 证明当前全局单选二级分类没有兑现成本，但少数精确槽有明确价值。应缩小其职责，不应整体删除。
4. **HL-Mem 与 Mem0 的差异是否影响策略？** 是。HL-Mem 的属性会改变冲突、去重和 TTL 行为，必须高精度、可 abstain；Mem0 的可选标签主要辅助文本记忆检索，容错空间更大。
5. **最佳方案？** 短期 C 用于建立可测基线，长期 E 做职责分离。A 只能按证据增量吸收为 tags 或少数新 slot；B 可用于 tags 层；D 不应采用。
